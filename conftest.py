"""Configuração da suíte: põe a raiz no sys.path e desliga a rede nos testes.

Um token de bot vivo mora no .env ao lado deste arquivo. Nenhum teste pode
sair para a rede por acidente — quem precisar de resposta troca o stub pelo
seu próprio no corpo do teste.
"""

import pytest
import requests

import monitor_milhas as mm


@pytest.fixture(autouse=True)
def _sem_rede(monkeypatch):
    def _proibido(*args, **kwargs):
        raise AssertionError("teste tentou usar a rede de verdade")

    monkeypatch.setattr(requests, "post", _proibido)
    monkeypatch.setattr(mm.feedparser, "parse", _proibido)
