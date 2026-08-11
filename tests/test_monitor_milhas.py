"""Testes de monitor_milhas.py — funções puras e o filtro de varredura."""

import json

import monitor_milhas as mm


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

    assert json.loads(arquivo.read_text(encoding="utf-8")) == ordem[-3:]


def test_carregar_estado_ausente_devolve_vazio(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "STATE_FILE", tmp_path / "nao-existe.json")
    assert mm.carregar_estado() == {}


def test_carregar_estado_ilegivel_recomeca(tmp_path, monkeypatch):
    arquivo = tmp_path / "estado.json"
    arquivo.write_text("{lixo,", encoding="utf-8")
    monkeypatch.setattr(mm, "STATE_FILE", arquivo)
    assert mm.carregar_estado() == {}


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


def test_normalizar_e_idempotente():
    uma_vez = mm._normalizar("Transferência Bonificada")
    assert mm._normalizar(uma_vez) == uma_vez
    assert uma_vez == "transferencia bonificada"
