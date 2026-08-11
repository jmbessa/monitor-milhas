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
