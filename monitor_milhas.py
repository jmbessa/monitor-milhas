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

Dependências:
    pip install feedparser requests
"""

from __future__ import annotations

import json
import os
import re
import sys
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

STATE_FILE = Path(__file__).with_name(".monitor_state.json")
ENV_FILE = Path(__file__).with_name(".env")
MAX_STATE_ENTRIES = 2000
USER_AGENT = "monitor-milhas/1.0 (uso pessoal)"


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
        if self.bonus_pct is not None and self.bonus_pct >= 70:
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
            for prog, mult in (("Smiles", 1 + self.bonus_pct / 100),
                               ("LATAM", 1 + self.bonus_pct / 100)):
                efetivo = MILHEIRO_ALVO / mult
                linhas.append(f"  ↳ a R$ {MILHEIRO_ALVO:.0f}/1k Livelo "
                              f"= R$ {efetivo:.2f}/1k {prog}")
                break  # mostra uma linha genérica, não duplica

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
    r"milheiro\s+(?:a partir de|por|de)\s+R\$\s*(\d{1,3}(?:[.,]\d{2})?)",
    re.IGNORECASE,
)
_RE_BONUS = re.compile(r"(\d{2,3})\s*%\s*(?:de\s+)?bonus", re.IGNORECASE)


def _to_float(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def extrair_milheiro(texto: str) -> float | None:
    """Menor milheiro citado no texto — promoções costumam citar o melhor caso."""
    texto = _normalizar(texto)
    valores = [_to_float(m) for m in _RE_MILHEIRO.findall(texto)]
    valores += [_to_float(m) for m in _RE_MILHEIRO_ALT.findall(texto)]
    valores = [v for v in valores if 5.0 <= v <= 100.0]  # sanidade
    return min(valores) if valores else None


def extrair_bonus(texto: str) -> int | None:
    """Maior percentual de bônus citado."""
    valores = [int(m) for m in _RE_BONUS.findall(_normalizar(texto))]
    valores = [v for v in valores if 10 <= v <= 200]
    return max(valores) if valores else None


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
    """IDs já vistos, como conjunto ordenado — dict preserva ordem de inserção."""
    if not STATE_FILE.exists():
        return {}
    try:
        dados = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] estado ilegível ({e}), recomeçando", file=sys.stderr)
        return {}
    return dict.fromkeys(dados)


def salvar_estado(vistos: dict[str, None]) -> None:
    # Corta pelo começo: descarta os mais antigos e preserva os recentes.
    # Só funciona porque `vistos` é dict — com set a ordem seria arbitrária.
    recortado = list(vistos)[-MAX_STATE_ENTRIES:]
    try:
        STATE_FILE.write_text(json.dumps(recortado), encoding="utf-8")
    except OSError as e:
        print(f"[erro] não salvou estado: {e}", file=sys.stderr)


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
        print(f"[erro] Telegram: {e}", file=sys.stderr)
        return False


# ----------------------------------------------------------------------------
# LOOP PRINCIPAL
# ----------------------------------------------------------------------------

def varrer() -> list[Alerta]:
    vistos = carregar_estado()
    novos: list[Alerta] = []

    for fonte, url in FEEDS:
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
        except Exception as e:
            print(f"[erro] {fonte}: {e}", file=sys.stderr)
            continue

        if getattr(feed, "bozo", False) and not feed.entries:
            print(f"[warn] {fonte}: feed vazio ou inválido", file=sys.stderr)
            continue

        for entrada in feed.entries[:30]:
            uid = entrada.get("id") or entrada.get("link")
            if not uid or uid in vistos:
                continue
            vistos[uid] = None

            titulo = entrada.get("title", "").strip()
            resumo = re.sub(r"<[^>]+>", " ", entrada.get("summary", ""))
            texto = f"{titulo}\n{resumo}"

            score, termos = pontuar(texto)
            if score < SCORE_MINIMO:
                continue

            novos.append(Alerta(
                fonte=fonte,
                titulo=titulo,
                link=entrada.get("link", ""),
                score=score,
                milheiro=extrair_milheiro(texto),
                bonus_pct=extrair_bonus(texto),
                termos=termos,
            ))

        time.sleep(1)  # educado com os servidores

    salvar_estado(vistos)
    # urgentes primeiro, depois por score
    novos.sort(key=lambda a: (a.urgente, a.score), reverse=True)
    return novos


def main() -> int:
    carregar_env()
    agora = datetime.now(timezone.utc).astimezone()
    alertas = varrer()

    if not alertas:
        print(f"[{agora:%d/%m %H:%M}] nada novo")
        return 0

    print(f"[{agora:%d/%m %H:%M}] {len(alertas)} alerta(s)")
    for a in alertas:
        enviar_telegram(a.formatar())
        time.sleep(0.5)  # rate limit do Telegram
    return 0


if __name__ == "__main__":
    sys.exit(main())
