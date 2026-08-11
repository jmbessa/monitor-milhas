"""Testes de monitor_milhas.py — funções puras e o filtro de varredura."""

import json
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

import monitor_milhas as mm

RAIZ = Path(mm.__file__).parent


# ---------------------------------------------------------------------------
# APOIO
# ---------------------------------------------------------------------------

def _entrada(uid="post-1", titulo="Livelo com 100% de bônus na transferência"):
    return {
        "id": uid,
        "title": titulo,
        "summary": "",
        "link": f"https://exemplo.com/{uid}",
    }


def _stub_feed(monkeypatch, entradas):
    """Todos os FEEDS passam a devolver estas entradas, sem tocar na rede."""
    feed = types.SimpleNamespace(entries=list(entradas), bozo=False)
    monkeypatch.setattr(mm.feedparser, "parse", lambda url, agent=None: feed)


@pytest.fixture
def varredura_isolada(tmp_path, monkeypatch):
    """Um feed de mentira e o estado num arquivo descartável."""
    monkeypatch.setattr(mm, "STATE_FILE", tmp_path / "estado.json")
    monkeypatch.setattr(mm, "FEEDS", [("Teste", "https://exemplo.com/feed")])
    monkeypatch.setattr(mm.time, "sleep", lambda _: None)
    return tmp_path


@pytest.fixture
def main_isolado(varredura_isolada, monkeypatch):
    """main() sem ler o .env de verdade e com credenciais de brinquedo."""
    monkeypatch.setattr(mm, "carregar_env", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-de-teste")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    return varredura_isolada


# ---------------------------------------------------------------------------
# ESTADO
# ---------------------------------------------------------------------------

def test_estado_sobrevive_ao_ciclo_preservando_os_mais_recentes(tmp_path, monkeypatch):
    """Carregar e salvar deve descartar os antigos, nunca os recentes."""
    arquivo = tmp_path / "estado.json"
    monkeypatch.setattr(mm, "STATE_FILE", arquivo)
    monkeypatch.setattr(mm, "MAX_STATE_ENTRIES", 3)

    ordem = [f"id-{i}" for i in range(20)]
    arquivo.write_text(json.dumps(ordem), encoding="utf-8")

    vistos = mm.carregar_estado()
    mm.salvar_estado(vistos)

    conteudo = arquivo.read_text(encoding="utf-8")
    assert json.loads(conteudo) == ordem[-3:]
    assert mm.carregar_estado() == dict.fromkeys(ordem[-3:])  # round-trip completo


def test_salvar_estado_grava_um_id_por_linha(tmp_path, monkeypatch):
    """~90KB numa linha só é diff ilegível e conflito impossível de resolver."""
    arquivo = tmp_path / "estado.json"
    monkeypatch.setattr(mm, "STATE_FILE", arquivo)

    mm.salvar_estado(dict.fromkeys(["id-a", "id-b", "id-c"]))

    conteudo = arquivo.read_text(encoding="utf-8")
    assert "\n" in conteudo
    linhas_com_id = [l for l in conteudo.splitlines() if "id-" in l]
    assert len(linhas_com_id) == 3  # cada id na sua própria linha


def test_carregar_estado_ausente_devolve_vazio(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "STATE_FILE", tmp_path / "nao-existe.json")
    assert mm.carregar_estado() == {}


def test_carregar_estado_ilegivel_aborta(tmp_path, monkeypatch):
    """I3: assumir vazio ressuscitaria a janela inteira e viraria enxurrada."""
    arquivo = tmp_path / "estado.json"
    arquivo.write_text("{lixo,", encoding="utf-8")
    monkeypatch.setattr(mm, "STATE_FILE", arquivo)
    with pytest.raises(mm.EstadoCorrompido):
        mm.carregar_estado()


def test_carregar_estado_com_conteudo_de_outro_tipo_aborta(tmp_path, monkeypatch):
    """JSON válido mas não uma lista de ids — dict.fromkeys aceitaria calado."""
    arquivo = tmp_path / "estado.json"
    arquivo.write_text('{"vistos": 3}', encoding="utf-8")
    monkeypatch.setattr(mm, "STATE_FILE", arquivo)
    with pytest.raises(mm.EstadoCorrompido):
        mm.carregar_estado()


def test_salvar_estado_preserva_o_arquivo_quando_a_escrita_falha(tmp_path, monkeypatch):
    """I3: escrita atômica — o estado anterior sobrevive a uma falha no meio."""
    arquivo = tmp_path / "estado.json"
    arquivo.write_text(json.dumps(["id-antigo"]), encoding="utf-8")
    monkeypatch.setattr(mm, "STATE_FILE", arquivo)

    def _falha_no_meio(dados, saida, **kwargs):
        saida.write('["id-nov')       # metade do JSON, só no temporário
        raise OSError("sem espaço em disco")

    monkeypatch.setattr(mm.json, "dump", _falha_no_meio)
    mm.salvar_estado(dict.fromkeys(["id-novo"]))

    assert json.loads(arquivo.read_text(encoding="utf-8")) == ["id-antigo"]
    assert list(tmp_path.iterdir()) == [arquivo]   # nenhum temporário largado


# ---------------------------------------------------------------------------
# NORMALIZAÇÃO DE ACENTO
# ---------------------------------------------------------------------------

def test_pontuar_casa_termo_escrito_sem_acento():
    score, termos = mm.pontuar("Livelo com bonus na transferencia")
    assert "bonus na transferencia" in termos
    assert score >= 10


def test_pontuar_indiferente_ao_acento():
    com_acento, _ = mm.pontuar("bônus na transferência")
    sem_acento, _ = mm.pontuar("bonus na transferencia")
    assert com_acento == sem_acento


def test_extrair_bonus_casa_sem_acento():
    assert mm.extrair_bonus("promocao de 80% de bonus") == 80


def test_extrair_bonus_ainda_casa_com_acento():
    assert mm.extrair_bonus("promoção de 80% de bônus") == 80


def test_extrair_bonus_com_bonus_antes_do_numero_e_ate():
    assert mm.extrair_bonus("Livelo: bônus de até 110% na transferência") == 110


def test_extrair_bonus_com_bonus_antes_do_numero():
    assert mm.extrair_bonus("bônus de 80% para Smiles") == 80


def test_normalizar_e_idempotente():
    uma_vez = mm._normalizar("Transferência Bonificada")
    assert mm._normalizar(uma_vez) == uma_vez
    assert uma_vez == "transferencia bonificada"


# ---------------------------------------------------------------------------
# EXTRAÇÃO — MILHEIRO
# ---------------------------------------------------------------------------

def test_extrair_milheiro_com_ponto_decimal():
    """'27.50' não é milhar — a captura é no máximo \\d{1,3}[.,]\\d{2}."""
    assert mm.extrair_milheiro("Compre pontos por R$ 27.50 o milheiro") == 27.5


def test_extrair_milheiro_com_virgula_decimal_continua_igual():
    assert mm.extrair_milheiro("Compre pontos por R$ 27,50 o milheiro") == 27.5


def test_extrair_milheiro_ponto_como_milhar_e_filtrado_pela_sanidade():
    """'1.000' truncado na captura vira 1.00 = R$ 1,00 — abaixo do piso de R$ 5."""
    assert mm.extrair_milheiro("R$ 1.000 por 1.000 pontos") is None


def test_extrair_milheiro_com_preposicao_a():
    assert mm.extrair_milheiro("Clube Livelo com milheiro a R$ 24,90") == 24.9


def test_extrair_milheiro_a_partir_de_continua_funcionando():
    """'a partir de' precisa vencer o 'a' isolado na alternação do regex."""
    assert mm.extrair_milheiro("milheiro a partir de R$ 28") == 28.0


# ---------------------------------------------------------------------------
# ALERTA — PREÇO EFETIVO NO BLOCO DE BÔNUS
# ---------------------------------------------------------------------------

def test_formatar_preco_efetivo_usa_o_milheiro_extraido():
    """Com milheiro extraído, o efetivo tem que sair dele — não do alvo fixo."""
    alerta = mm.Alerta(
        fonte="Teste", titulo="T", link="https://x", score=10,
        milheiro=27.5, bonus_pct=100,
    )
    assert "13.75" in alerta.formatar()


def test_formatar_preco_efetivo_cai_no_alvo_sem_milheiro():
    alerta = mm.Alerta(
        fonte="Teste", titulo="T", link="https://x", score=10,
        milheiro=None, bonus_pct=100,
    )
    assert "12.50" in alerta.formatar()


# ---------------------------------------------------------------------------
# ALERTA — URGÊNCIA POR BÔNUS (BONUS_URGENTE)
# ---------------------------------------------------------------------------

def test_urgente_no_limiar_de_bonus_urgente():
    assert mm.BONUS_URGENTE == 70
    alerta = mm.Alerta(
        fonte="Teste", titulo="T", link="https://x", score=0,
        milheiro=None, bonus_pct=70,
    )
    assert alerta.urgente is True


def test_urgente_abaixo_do_limiar_de_bonus_urgente_nao_urgentiza():
    alerta = mm.Alerta(
        fonte="Teste", titulo="T", link="https://x", score=0,
        milheiro=None, bonus_pct=69,
    )
    assert alerta.urgente is False


# ---------------------------------------------------------------------------
# SINAL FORTE
# ---------------------------------------------------------------------------

def test_sinal_forte_com_milheiro_dentro_do_teto():
    assert mm.sinal_forte(27.0, None) is True


def test_sinal_forte_no_teto_exato():
    """Fronteira: o teto é inclusivo — R$ 30,00 o milheiro ainda é sinal."""
    assert mm.MILHEIRO_TETO == 30.0
    assert mm.sinal_forte(30.0, None) is True


def test_sinal_forte_ignora_milheiro_acima_do_teto():
    assert mm.sinal_forte(45.0, None) is False


def test_sinal_forte_com_bonus_alto():
    assert mm.sinal_forte(None, 70) is True


def test_sinal_forte_no_bonus_exato():
    """Fronteira: 50% de bônus, o valor de BONUS_FORTE, já é sinal."""
    assert mm.BONUS_FORTE == 50
    assert mm.sinal_forte(None, 50) is True


def test_sinal_forte_ignora_bonus_pequeno():
    assert mm.sinal_forte(None, 40) is False


def test_sinal_forte_sem_sinal_nenhum():
    assert mm.sinal_forte(None, None) is False


def test_varrer_alerta_post_de_score_baixo_que_anuncia_milheiro(tmp_path, monkeypatch):
    """'milheiro por R$ 27' pontua 8, abaixo do corte — mas o fato tem precedência."""
    monkeypatch.setattr(mm, "STATE_FILE", tmp_path / "estado.json")
    monkeypatch.setattr(mm, "FEEDS", [("Teste", "https://exemplo.com/feed")])
    monkeypatch.setattr(mm.time, "sleep", lambda _: None)

    entrada = {
        "id": "post-1",
        "title": "Oferta relâmpago: milheiro por R$ 27",
        "summary": "",
        "link": "https://exemplo.com/post-1",
    }
    feed = types.SimpleNamespace(entries=[entrada], bozo=False)
    monkeypatch.setattr(mm.feedparser, "parse", lambda url, agent=None: feed)

    alertas, _ = mm.varrer()

    assert len(alertas) == 1
    assert alertas[0].milheiro == 27.0
    assert alertas[0].score < mm.SCORE_MINIMO


def test_varrer_ignora_post_irrelevante(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "STATE_FILE", tmp_path / "estado.json")
    monkeypatch.setattr(mm, "FEEDS", [("Teste", "https://exemplo.com/feed")])
    monkeypatch.setattr(mm.time, "sleep", lambda _: None)

    entrada = {
        "id": "post-2",
        "title": "As 10 melhores praias do Nordeste",
        "summary": "",
        "link": "https://exemplo.com/post-2",
    }
    feed = types.SimpleNamespace(entries=[entrada], bozo=False)
    monkeypatch.setattr(mm.feedparser, "parse", lambda url, agent=None: feed)

    alertas, _ = mm.varrer()
    assert alertas == []


# ---------------------------------------------------------------------------
# I4 — SCORE NEGATIVO VENCE O SINAL FORTE
# ---------------------------------------------------------------------------

def test_varrer_descarta_score_negativo_ainda_que_o_preco_seja_legivel(
    varredura_isolada, monkeypatch
):
    """'esfera', 'seguro' e 'celular' existem para matar exatamente isto."""
    titulo = "Esfera: pontos a R$ 27 por 1.000 no seguro celular"
    assert mm.extrair_milheiro(titulo) == 27.0     # o preço continua legível
    assert mm.pontuar(titulo)[0] < 0               # e o dicionário falou contra

    _stub_feed(monkeypatch, [_entrada("post-neg", titulo)])
    alertas, _ = mm.varrer()
    assert alertas == []


def test_varrer_ainda_alerta_score_positivo_com_o_mesmo_preco(
    varredura_isolada, monkeypatch
):
    """O sinal forte segue furando o corte quando o dicionário só ficou curto."""
    titulo = "Oferta relâmpago: milheiro por R$ 27"
    _stub_feed(monkeypatch, [_entrada("post-pos", titulo)])

    alertas, _ = mm.varrer()

    assert len(alertas) == 1
    assert alertas[0].score == 8                   # abaixo de SCORE_MINIMO
    assert alertas[0].milheiro == 27.0


# ---------------------------------------------------------------------------
# C1 — ENVIO QUE FALHA TEM QUE FICAR VERMELHO
# ---------------------------------------------------------------------------

def test_main_falha_quando_o_envio_do_telegram_falha(main_isolado, monkeypatch):
    """Estado só pode ser commitado se o alerta chegou ao dono."""
    _stub_feed(monkeypatch, [_entrada()])
    monkeypatch.setattr(mm, "enviar_telegram", lambda mensagem: False)

    codigo = mm.main()

    assert codigo != 0
    assert codigo == mm.SAIDA_ENVIO


def test_main_devolve_zero_quando_o_envio_da_certo(main_isolado, monkeypatch):
    enviados = []
    _stub_feed(monkeypatch, [_entrada()])

    def _envia(mensagem):
        enviados.append(mensagem)
        return True

    monkeypatch.setattr(mm, "enviar_telegram", _envia)

    assert mm.main() == 0
    assert len(enviados) == 1


# ---------------------------------------------------------------------------
# C1 (residual) — ESTADO SÓ SE SALVA DEPOIS DO ENVIO
# ---------------------------------------------------------------------------

def test_main_falha_de_envio_nao_toca_no_arquivo_de_estado(main_isolado, monkeypatch):
    """Mutação: se salvar_estado voltar para dentro de varrer(), isto falha.

    Em cron local (sem o "descarte" do runner efêmero do Actions) uma escrita
    antes do envio perderia o alerta pra sempre: o id já teria sido marcado
    como visto quando o Telegram falhou.
    """
    arquivo = main_isolado / "estado.json"
    conteudo_anterior = json.dumps(["id-antigo"])
    arquivo.write_text(conteudo_anterior, encoding="utf-8")

    _stub_feed(monkeypatch, [_entrada()])
    monkeypatch.setattr(mm, "enviar_telegram", lambda mensagem: False)

    codigo = mm.main()

    assert codigo == mm.SAIDA_ENVIO
    assert arquivo.read_bytes() == conteudo_anterior.encode("utf-8")


def test_main_envio_bem_sucedido_salva_estado_com_o_novo_id(main_isolado, monkeypatch):
    arquivo = main_isolado / "estado.json"
    _stub_feed(monkeypatch, [_entrada("post-novo")])
    monkeypatch.setattr(mm, "enviar_telegram", lambda mensagem: True)

    codigo = mm.main()

    assert codigo == mm.SAIDA_OK
    assert json.loads(arquivo.read_text(encoding="utf-8")) == ["post-novo"]


def test_main_sem_alertas_ainda_assim_salva_estado(main_isolado, monkeypatch):
    """Post novo e irrelevante marca visto mesmo sem nenhum alerta enviado."""
    arquivo = main_isolado / "estado.json"
    entrada_irrelevante = {
        "id": "post-irrelevante",
        "title": "As 10 melhores praias do Nordeste",
        "summary": "",
        "link": "https://exemplo.com/post-irrelevante",
    }
    _stub_feed(monkeypatch, [entrada_irrelevante])

    codigo = mm.main()

    assert codigo == mm.SAIDA_OK
    assert json.loads(arquivo.read_text(encoding="utf-8")) == ["post-irrelevante"]


def test_main_falha_sem_credenciais_e_nem_chega_a_varrer(main_isolado, monkeypatch):
    """Secret com nome errado interpola vazio: nada pode ser consumido."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")

    def _nao_deveria(*args, **kwargs):
        raise AssertionError("varreu sem ter para quem alertar")

    monkeypatch.setattr(mm, "varrer", _nao_deveria)

    codigo = mm.main()

    assert codigo != 0
    assert codigo == mm.SAIDA_CREDENCIAIS


# ---------------------------------------------------------------------------
# C2 — FEEDS MUDOS TÊM QUE FICAR VERMELHOS
# ---------------------------------------------------------------------------

def test_main_falha_quando_nenhum_feed_devolve_entradas(main_isolado, monkeypatch):
    _stub_feed(monkeypatch, [])
    monkeypatch.setattr(mm, "enviar_telegram", lambda mensagem: True)

    codigo = mm.main()

    assert codigo != 0
    assert codigo == mm.SAIDA_FEEDS


def test_varrer_aborta_e_nao_salva_estado_com_todos_os_feeds_vazios(
    tmp_path, monkeypatch
):
    estado = tmp_path / "estado.json"
    monkeypatch.setattr(mm, "STATE_FILE", estado)
    monkeypatch.setattr(mm, "FEEDS", [("A", "https://a/feed"), ("B", "https://b/feed")])
    monkeypatch.setattr(mm.time, "sleep", lambda _: None)
    _stub_feed(monkeypatch, [])

    with pytest.raises(mm.FeedsIndisponiveis):
        mm.varrer()

    assert not estado.exists()


def test_main_falha_com_estado_corrompido(main_isolado, monkeypatch):
    (main_isolado / "estado.json").write_text('["a", "b', encoding="utf-8")
    _stub_feed(monkeypatch, [_entrada()])
    monkeypatch.setattr(mm, "enviar_telegram", lambda mensagem: True)

    codigo = mm.main()

    assert codigo != 0
    assert codigo == mm.SAIDA_ESTADO


# ---------------------------------------------------------------------------
# I1 / M2 / M4 / M5 — ROBUSTEZ
# ---------------------------------------------------------------------------

def test_timeout_de_socket_vale_desde_a_importacao():
    """I1: feedparser não aceita timeout; um host mudo travaria o job."""
    assert mm.TIMEOUT_SOCKET == 20
    assert socket.getdefaulttimeout() == mm.TIMEOUT_SOCKET


def test_erro_do_telegram_nao_vaza_o_token(monkeypatch, capsys):
    """M2: o texto do requests traz a URL inteira, com o token dentro."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SEGREDO")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def _estoura(*args, **kwargs):
        raise mm.requests.HTTPError(
            "401 Client Error: Unauthorized for url: "
            "https://api.telegram.org/bot123456:SEGREDO/sendMessage"
        )

    monkeypatch.setattr(mm.requests, "post", _estoura)

    assert mm.enviar_telegram("oi") is False

    saida = capsys.readouterr()
    assert "SEGREDO" not in saida.out + saida.err
    assert "HTTPError" in saida.err


class _EntradaTorta:
    """Entrada que não se comporta como dicionário."""

    def get(self, *args, **kwargs):
        raise ValueError("entrada sem forma de dicionário")


def test_varrer_pula_entrada_malformada_e_segue(varredura_isolada, monkeypatch, capsys):
    """M5: uma entrada ruim ficaria dias no feed, quebrando toda execução."""
    _stub_feed(monkeypatch, [_EntradaTorta(), _entrada("post-bom")])

    alertas, _ = mm.varrer()

    assert len(alertas) == 1
    assert alertas[0].link.endswith("post-bom")
    assert "entrada ignorada" in capsys.readouterr().err


def test_alerta_com_emoji_sobrevive_a_saida_cp1252():
    """M4: `>> monitor.log` no Windows dá cp1252, e o 🔴 quebraria o print."""
    codigo = (
        "import monitor_milhas as mm; "
        "print(mm.Alerta('Fonte', 'Titulo', 'https://x', 30, milheiro=20.0)"
        ".formatar())"
    )
    ambiente = {**os.environ, "PYTHONIOENCODING": "cp1252"}

    r = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=str(RAIZ), env=ambiente, capture_output=True,
    )

    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "URGENTE" in r.stdout.decode("utf-8", "replace")
