#!/usr/bin/env python3
"""
monitor_milhas.py — Monitor de promoções de pontos/milhas via RSS.

Vigia os principais blogs de milhas, pontua relevância por palavra-chave,
extrai o milheiro anunciado quando possível e alerta no Telegram.

Uso:
    python monitor_milhas.py

Credenciais: crie um .env ao lado deste arquivo com

    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=987654321

Variáveis já presentes no ambiente têm precedência sobre o .env.

Cron sugerido (a cada 20 min, 7h-23h):
    */20 7-23 * * * cd /path && /usr/bin/python3 monitor_milhas.py >> monitor.log 2>&1

Códigos de saída — o workflow depende deles: qualquer valor diferente de zero
derruba o passo da varredura e impede o commit do estado, para que a execução
seguinte reencontre os posts como novos e tente outra vez.

    0  fim normal, com ou sem alertas
    1  credenciais do Telegram ausentes
    2  algum alerta não foi entregue
    3  nenhum feed devolveu entradas
    4  arquivo de estado ilegível

Dependências:
    pip install feedparser requests
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import feedparser
    import requests
except ImportError:
    sys.exit("Faltam dependências. Rode: pip install feedparser requests")

# Os emojis do alerta quebram no cp1252 do Windows quando a saída vai para um
# pipe ou arquivo (`>> monitor.log`). Sem isto, `print()` levanta
# UnicodeEncodeError e derruba a execução.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):  # stdout trocado por objeto sem suporte
        pass


# ----------------------------------------------------------------------------
# CONFIGURAÇÃO
# ----------------------------------------------------------------------------

FEEDS = [
    ("Melhores Cartões", "https://www.melhorescartoes.com.br/feed"),
    ("Melhores Destinos", "https://www.melhoresdestinos.com.br/feed"),
    ("Passageiro de Primeira", "https://passageirodeprimeira.com/feed"),
    ("Pontos pra Voar", "https://pontospravoar.com/feed"),
]

# Palavras-chave com peso. Score = soma dos pesos dos termos encontrados.
# Ajuste os pesos conforme o que você quer priorizar.
KEYWORDS: dict[str, int] = {
    # --- Transferência: variações reais de manchete ---
    "transferência bonificada": 10,
    "bônus na transferência": 10,
    "bônus de transferência": 10,
    "bonificada": 8,
    "transferir pontos": 6,
    "transferência de pontos": 6,
    "% de bônus": 7,          # pega "80% de bônus", "até 100% de bônus"
    "% bônus": 7,
    "bônus": 3,               # peso baixo: só soma, nunca decide sozinho
    "pontos + dinheiro": 8,
    "pontos mais dinheiro": 8,

    # --- Compra de pontos ---
    "compra de pontos": 8,
    "comprar pontos": 8,
    "compre pontos": 8,
    "pontos com desconto": 8,
    "milheiro": 8,
    "% off": 5,
    "% de desconto": 5,
    "turbo livelo": 7,

    # --- Clube ---
    "clube livelo": 7,
    "assine o clube": 7,
    "upgrade de plano": 6,
    "pontos extras": 5,

    # --- Sazonais ---
    "black friday": 10,
    "mês do consumidor": 8,
    "aniversário livelo": 8,

    # --- Programas ---
    "livelo": 6,
    "latam pass": 5,
    "smiles": 5,

    # --- Rota ---
    "japão": 6,
    "tóquio": 6,
    "executiva": 4,

    # --- Ruído ---
    "cashback": -3,
    "esfera": -4,
    "azul fidelidade": -2,
    "seguro": -3,
    "hotel": -2,
    "boas-vindas": -4,        # "bônus de boas-vindas" de cartão
    "streaming": -4,
    "celular": -4,
}


def _normalizar(s: str) -> str:
    """Minúsculas e sem acento — protege contra variação editorial ("bonus")."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower())
        if unicodedata.category(c) != "Mn"
    )


# KEYWORDS fica legível com acento no fonte; a comparação usa esta versão.
_KEYWORDS_NORM: dict[str, int] = {_normalizar(k): v for k, v in KEYWORDS.items()}

if len(_KEYWORDS_NORM) != len(KEYWORDS):
    print("[warn] chaves de KEYWORDS colidem ao perder o acento", file=sys.stderr)

SCORE_MINIMO = 14          # abaixo disso, ignora
MILHEIRO_ALVO = 25.0       # R$ por 1.000 pontos Livelo — alerta URGENTE abaixo disso
MILHEIRO_TETO = 30.0       # acima disso, nunca comprar
BONUS_FORTE = 50           # % de bônus que fura o corte por score (sinal forte)
BONUS_URGENTE = 70         # % de bônus que marca o alerta como urgente

STATE_FILE = Path(__file__).with_name(".monitor_state.json")
ENV_FILE = Path(__file__).with_name(".env")
MAX_STATE_ENTRIES = 2000
USER_AGENT = "monitor-milhas/1.0 (uso pessoal)"
TIMEOUT_SOCKET = 20        # segundos — feedparser não expõe timeout próprio

# Um blog que aceita a conexão e nunca responde travaria a varredura até o
# limite do job (horas), enfileirando as execuções seguintes atrás dela.
socket.setdefaulttimeout(TIMEOUT_SOCKET)

# Códigos de saída — ver o cabeçalho do módulo.
SAIDA_OK = 0
SAIDA_CREDENCIAIS = 1
SAIDA_ENVIO = 2
SAIDA_FEEDS = 3
SAIDA_ESTADO = 4


class EstadoCorrompido(RuntimeError):
    """Estado ilegível: assumir vazio ressuscitaria a janela inteira de posts."""


class FeedsIndisponiveis(RuntimeError):
    """Nenhum feed devolveu entrada — CDN bloqueando o runner, ou rede fora."""


def carregar_env() -> None:
    """Lê o .env ao lado do script. Não sobrescreve o que já veio do ambiente."""
    if not ENV_FILE.exists():
        return
    try:
        conteudo = ENV_FILE.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[warn] .env ilegível ({e})", file=sys.stderr)
        return

    for linha in conteudo.splitlines():
        linha = linha.strip().removeprefix("export ").strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip()
        valor = valor.strip().strip("\"'")
        if chave and chave not in os.environ:
            os.environ[chave] = valor


# ----------------------------------------------------------------------------
# MODELO
# ----------------------------------------------------------------------------

@dataclass
class Alerta:
    fonte: str
    titulo: str
    link: str
    score: int
    milheiro: float | None = None
    bonus_pct: int | None = None
    termos: list[str] = field(default_factory=list)

    @property
    def urgente(self) -> bool:
        if self.milheiro is not None and self.milheiro <= MILHEIRO_ALVO:
            return True
        if self.bonus_pct is not None and self.bonus_pct >= BONUS_URGENTE:
            return True
        return self.score >= 30

    def formatar(self) -> str:
        prefixo = "🔴 URGENTE" if self.urgente else "🟡"
        linhas = [f"{prefixo} — {self.fonte}", "", self.titulo]

        if self.milheiro is not None:
            if self.milheiro <= MILHEIRO_ALVO:
                veredito = "COMPRAR"
            elif self.milheiro <= MILHEIRO_TETO:
                veredito = "aceitável"
            else:
                veredito = "CARO — passar"
            linhas.append(f"\nMilheiro: R$ {self.milheiro:.2f} → {veredito}")

        if self.bonus_pct is not None:
            linhas.append(f"Bônus de transferência: {self.bonus_pct}%")
            base = self.milheiro if self.milheiro is not None else MILHEIRO_ALVO
            mult = 1 + self.bonus_pct / 100
            efetivo = base / mult
            linhas.append(f"  ↳ a R$ {base:.2f}/1k Livelo "
                          f"= R$ {efetivo:.2f}/1k transferido")

        linhas.append(f"\nScore: {self.score} ({', '.join(self.termos[:5])})")
        linhas.append(self.link)
        return "\n".join(linhas)


# ----------------------------------------------------------------------------
# EXTRAÇÃO
# ----------------------------------------------------------------------------

_RE_MILHEIRO = re.compile(
    r"R\$\s*(\d{1,3}(?:[.,]\d{2})?)\s*(?:o\s+)?"
    r"(?:milheiro|a cada 1\.?000|por 1\.?000|/\s*1\.?000)",
    re.IGNORECASE,
)
_RE_MILHEIRO_ALT = re.compile(
    r"milheiro\s+(?:a partir de|por|de|a)\s+R\$\s*(\d{1,3}(?:[.,]\d{2})?)",
    re.IGNORECASE,
)
_RE_BONUS = re.compile(r"(\d{2,3})\s*%\s*(?:de\s+)?bonus", re.IGNORECASE)
_RE_BONUS_INV = re.compile(r"bonus\s+(?:de\s+)?(?:ate\s+)?(\d{2,3})\s*%", re.IGNORECASE)


def _to_float(s: str) -> float:
    # A captura é no máximo \d{1,3}[.,]\d{2} — separador aqui é sempre decimal.
    return float(s.replace(",", "."))


def extrair_milheiro(texto: str) -> float | None:
    """Menor milheiro citado no texto — promoções costumam citar o melhor caso."""
    texto = _normalizar(texto)
    valores = [_to_float(m) for m in _RE_MILHEIRO.findall(texto)]
    valores += [_to_float(m) for m in _RE_MILHEIRO_ALT.findall(texto)]
    valores = [v for v in valores if 5.0 <= v <= 100.0]  # sanidade
    return min(valores) if valores else None


def extrair_bonus(texto: str) -> int | None:
    """Maior percentual de bônus citado — número antes ou depois de "bônus"."""
    baixo = _normalizar(texto)
    valores = [int(m) for m in _RE_BONUS.findall(baixo)]
    valores += [int(m) for m in _RE_BONUS_INV.findall(baixo)]
    valores = [v for v in valores if 10 <= v <= 200]
    return max(valores) if valores else None


def sinal_forte(milheiro: float | None, bonus_pct: int | None) -> bool:
    """Milheiro ou bônus extraído é fato; o score é palpite. Fato tem precedência."""
    if milheiro is not None and milheiro <= MILHEIRO_TETO:
        return True
    return bonus_pct is not None and bonus_pct >= BONUS_FORTE


def pontuar(texto: str) -> tuple[int, list[str]]:
    baixo = _normalizar(texto)
    score, achados = 0, []
    for termo, peso in _KEYWORDS_NORM.items():
        if termo in baixo:
            score += peso
            if peso > 0:
                achados.append(termo)
    return score, achados


# ----------------------------------------------------------------------------
# ESTADO
# ----------------------------------------------------------------------------

def carregar_estado() -> dict[str, None]:
    """IDs já vistos, como conjunto ordenado — dict preserva ordem de inserção.

    Estado ilegível aborta a execução em vez de recomeçar do zero: um arquivo
    truncado seria lido como "nada visto" e a janela inteira viraria alerta, a
    enxurrada de dezenas de mensagens que a semeadura existe para evitar.
    """
    if not STATE_FILE.exists():
        return {}
    try:
        dados = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise EstadoCorrompido(f"{STATE_FILE.name} ilegível: {e}") from e
    if not isinstance(dados, list) or not all(isinstance(i, str) for i in dados):
        raise EstadoCorrompido(f"{STATE_FILE.name} não é uma lista de ids")
    return dict.fromkeys(dados)


def salvar_estado(vistos: dict[str, None]) -> None:
    """Grava o estado de forma atômica: temporário ao lado, depois `os.replace`.

    Escrever por cima truncaria o arquivo antes de escrever — uma interrupção
    no meio (preempção do runner) deixaria um JSON pela metade.
    """
    # Corta pelo começo: descarta os mais antigos e preserva os recentes.
    # Só funciona porque `vistos` é dict — com set a ordem seria arbitrária.
    recortado = list(vistos)[-MAX_STATE_ENTRIES:]
    temporario: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=STATE_FILE.parent,
            prefix=STATE_FILE.name + ".",
            suffix=".tmp",
            delete=False,
        ) as saida:
            temporario = Path(saida.name)
            json.dump(recortado, saida, indent=0)
            saida.flush()
            os.fsync(saida.fileno())
        os.replace(temporario, STATE_FILE)
    except OSError as e:
        print(f"[erro] não salvou estado: {e}", file=sys.stderr)
        if temporario is not None:
            temporario.unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------

def enviar_telegram(mensagem: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[dry-run] TELEGRAM_* não configurado\n" + mensagem + "\n" + "-" * 50)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": mensagem,
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        # Só o nome da exceção e o status: o texto do requests traz a URL
        # inteira, com o token dentro, e o log do Actions é público.
        status = getattr(getattr(e, "response", None), "status_code", None)
        detalhe = f" (HTTP {status})" if status else ""
        print(f"[erro] Telegram: {type(e).__name__}{detalhe}", file=sys.stderr)
        return False


# ----------------------------------------------------------------------------
# LOOP PRINCIPAL
# ----------------------------------------------------------------------------

def _processar_entrada(fonte: str, entrada: dict, vistos: dict[str, None]) -> Alerta | None:
    """Alerta da entrada, ou None se ela já foi vista ou não interessa."""
    uid = entrada.get("id") or entrada.get("link")
    if not uid or uid in vistos:
        return None
    vistos[uid] = None

    titulo = entrada.get("title", "").strip()
    resumo = re.sub(r"<[^>]+>", " ", entrada.get("summary", ""))
    texto = f"{titulo}\n{resumo}"

    score, termos = pontuar(texto)
    milheiro = extrair_milheiro(texto)
    bonus_pct = extrair_bonus(texto)

    # Sinal duro fura o corte: o dicionário pode estar incompleto, mas um
    # milheiro anunciado no título não deixa dúvida. Score negativo é outra
    # história — aí o dicionário não ficou calado, falou contra o post
    # ("seguro", "boas-vindas", "celular"), e a palavra dele vale.
    if score < SCORE_MINIMO and not (score >= 0 and sinal_forte(milheiro, bonus_pct)):
        return None

    return Alerta(
        fonte=fonte,
        titulo=titulo,
        link=entrada.get("link", ""),
        score=score,
        milheiro=milheiro,
        bonus_pct=bonus_pct,
        termos=termos,
    )


def varrer() -> tuple[list[Alerta], dict[str, None]]:
    vistos = carregar_estado()
    novos: list[Alerta] = []
    total_entradas = 0

    for fonte, url in FEEDS:
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
        except Exception as e:
            print(f"[erro] {fonte}: {e}", file=sys.stderr)
            continue

        entradas = list(getattr(feed, "entries", None) or [])
        if not entradas:
            print(f"[warn] {fonte}: feed vazio ou inválido", file=sys.stderr)
            continue
        total_entradas += len(entradas)

        for entrada in entradas[:30]:
            try:
                alerta = _processar_entrada(fonte, entrada, vistos)
            except Exception as e:
                # Uma entrada torta não pode derrubar a varredura inteira: ela
                # ficaria no feed por dias, quebrando toda execução.
                print(f"[warn] {fonte}: entrada ignorada "
                      f"({type(e).__name__}: {e})", file=sys.stderr)
                continue
            if alerta is not None:
                novos.append(alerta)

        time.sleep(1)  # educado com os servidores

    # Os quatro feeds mudos ao mesmo tempo não é semana fraca, é bloqueio:
    # sem isto o monitor diria "nada novo" por semanas, sempre verde.
    if total_entradas == 0:
        raise FeedsIndisponiveis("nenhum dos feeds devolveu entradas")

    # urgentes primeiro, depois por score
    novos.sort(key=lambda a: (a.urgente, a.score), reverse=True)
    return novos, vistos


def main() -> int:
    carregar_env()
    agora = datetime.now(timezone.utc).astimezone()

    # Antes de varrer: sem credenciais todo envio cairia no ramo dry-run, que
    # despeja o alerta no log — público, no Actions — devolve False e some com
    # o negócio, marcando o post como visto. Um secret com o nome errado é o
    # jeito mais provável de estrear, então tem que ficar vermelho na hora.
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        print("[erro] TELEGRAM_BOT_TOKEN e/ou TELEGRAM_CHAT_ID ausentes",
              file=sys.stderr)
        return SAIDA_CREDENCIAIS

    try:
        alertas, vistos = varrer()
    except FeedsIndisponiveis as e:
        print(f"[erro] {e}", file=sys.stderr)
        return SAIDA_FEEDS
    except EstadoCorrompido as e:
        print(f"[erro] {e}", file=sys.stderr)
        return SAIDA_ESTADO

    if not alertas:
        # Sem alerta ainda há posts novos e não-alarmantes para marcar como
        # vistos — sem isto eles voltariam a ser avaliados na próxima volta.
        salvar_estado(vistos)
        print(f"[{agora:%d/%m %H:%M}] nada novo")
        return SAIDA_OK

    print(f"[{agora:%d/%m %H:%M}] {len(alertas)} alerta(s)")
    falhas = 0
    for a in alertas:
        if not enviar_telegram(a.formatar()):
            falhas += 1
        time.sleep(0.5)  # rate limit do Telegram

    if falhas:
        # Sai vermelho de propósito e NÃO salva o estado: os posts que
        # falharam continuam "não vistos" e a próxima execução reencontra o
        # lote inteiro e tenta de novo. Duplicar alerta é ruído tolerável;
        # perdê-lo em silêncio não é — vale tanto na nuvem (o passo de
        # persistência simplesmente não roda) quanto num cron local, onde
        # antes desta correção o arquivo já tinha sido reescrito em disco.
        print(f"[erro] {falhas} de {len(alertas)} alerta(s) não entregues",
              file=sys.stderr)
        return SAIDA_ENVIO

    salvar_estado(vistos)
    return SAIDA_OK


if __name__ == "__main__":
    sys.exit(main())
