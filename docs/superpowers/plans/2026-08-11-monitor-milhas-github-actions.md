# Monitor de Milhas no GitHub Actions — Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fazer o `monitor_milhas.py` rodar sozinho a cada 30 minutos no GitHub Actions, sem perder alertas nem repetir os já enviados.

**Architecture:** O mesmo script roda local e na nuvem. As credenciais vêm do `.env` local ou dos GitHub Secrets (variável de ambiente sempre tem precedência sobre o `.env`). O `.monitor_state.json` passa a ser versionado: o job faz checkout dele, roda o script e commita o arquivo de volta quando muda — é o que impede alerta repetido. Junto vão três correções no script que só ficam caras na nuvem: truncamento de estado que descartava IDs ao acaso, dicionário cego a variação de acento, e corte por score que descartava post com milheiro anunciado.

**Tech Stack:** Python 3.12 (CI) / 3.14 (local), feedparser, requests, pytest, GitHub Actions.

## Global Constraints

- Dependências de runtime limitadas a `feedparser` e `requests`. Nada de `python-dotenv` — o `carregar_env()` da stdlib já cobre.
- O código roda sem alteração em Python 3.12 (CI, Ubuntu) e 3.14 (local, Windows).
- Nomes, comentários e docstrings em português, seguindo o estilo do arquivo existente.
- `.env` nunca entra em commit. Conferir com `git status` antes de cada commit.
- `.monitor_state.json` é versionado de propósito, a partir da Task 4.
- Formato do estado em disco continua sendo uma lista JSON de strings, compatível com o arquivo já existente.
- `MILHEIRO_TETO = 30.0` e `SCORE_MINIMO = 14` mantêm os valores atuais.

---

### Task 1: Infraestrutura de teste + truncamento ordenado do estado

`salvar_estado()` faz `list(vistos)[-MAX_STATE_ENTRIES:]` sobre um `set`. A ordem de iteração de um set é arbitrária, então o corte guarda 2.000 entradas quaisquer, não as 2.000 mais recentes. Na nuvem isso significa post antigo voltando a alertar.

**Files:**
- Create: `conftest.py` (vazio — faz o pytest colocar a raiz do repo no `sys.path`)
- Create: `requirements-dev.txt`
- Create: `tests/test_monitor_milhas.py`
- Modify: `monitor_milhas.py:242-257` (`carregar_estado`, `salvar_estado`)
- Modify: `monitor_milhas.py:310` (`vistos.add(uid)`)

**Interfaces:**
- Consumes: nada (primeira task)
- Produces: `carregar_estado() -> dict[str, None]`, `salvar_estado(vistos: dict[str, None]) -> None`. Tasks seguintes assumem que `vistos` é um dict usado como conjunto ordenado.

- [ ] **Step 1: Instalar o pytest**

```powershell
python -m pip install pytest
```

- [ ] **Step 2: Criar o `conftest.py` vazio na raiz**

Sem ele, `import monitor_milhas` de dentro de `tests/` falha: o pytest só coloca a raiz do repo no `sys.path` quando encontra um `conftest.py` ali.

```powershell
New-Item -ItemType File conftest.py
```

- [ ] **Step 3: Criar o `requirements-dev.txt`**

Só o que os testes precisam. As dependências de runtime ficam no `requirements.txt` (Task 4) e já estão instaladas nesta máquina.

```
pytest>=8.0
```

- [ ] **Step 4: Escrever o teste que falha**

Arquivo `tests/test_monitor_milhas.py`:

```python
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
```

- [ ] **Step 5: Rodar e confirmar que falha**

```powershell
python -m pytest tests/test_monitor_milhas.py -v
```

Esperado: os **três** falham.

- `test_estado_sobrevive_ao_ciclo_preservando_os_mais_recentes`: `carregar_estado` devolve `set`, a ordem se perde, e o corte pega 3 IDs ao acaso em vez de `["id-17", "id-18", "id-19"]`.
- Os outros dois: `carregar_estado` devolve `set()`, e `set() == {}` é `False` em Python — um set vazio não é igual a um dict vazio.

- [ ] **Step 6: Trocar o set por dict em `carregar_estado` e `salvar_estado`**

Substituir as duas funções por:

```python
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
```

- [ ] **Step 7: Ajustar a inserção em `varrer()`**

Em `monitor_milhas.py`, trocar:

```python
            vistos.add(uid)
```

por:

```python
            vistos[uid] = None
```

O teste `uid in vistos` logo acima não muda — funciona igual em dict.

- [ ] **Step 8: Rodar e confirmar que passa**

```powershell
python -m pytest tests/test_monitor_milhas.py -v
```

Esperado: 3 passed.

- [ ] **Step 9: Commit**

```powershell
git add conftest.py requirements-dev.txt tests/test_monitor_milhas.py monitor_milhas.py
git commit -m @'
fix: preserva as entradas mais recentes ao truncar o estado

O corte era feito sobre um set, cuja ordem de iteracao e arbitraria, entao
guardava 2000 IDs quaisquer em vez dos 2000 mais recentes. Na nuvem isso
faria post antigo voltar a alertar. Estado passa a ser dict ordenado.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
'@
```

---

### Task 2: Normalização de acento

Se um blog escrever "bonus" sem circunflexo, nenhuma chave acentuada de `KEYWORDS` casa e o post é descartado. O dicionário continua escrito com acento no fonte; a normalização acontece no carregamento.

**Files:**
- Modify: `monitor_milhas.py` — import de `unicodedata`, `_normalizar()`, `_KEYWORDS_NORM`, `_RE_BONUS`, `pontuar()`, `extrair_milheiro()`, `extrair_bonus()`
- Modify: `tests/test_monitor_milhas.py` (acrescentar seção)

**Interfaces:**
- Consumes: nada da Task 1
- Produces: `_normalizar(s: str) -> str` (idempotente), `_KEYWORDS_NORM: dict[str, int]`. `pontuar`, `extrair_milheiro` e `extrair_bonus` mantêm as assinaturas atuais e passam a normalizar internamente o próprio argumento.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar ao fim de `tests/test_monitor_milhas.py`:

```python
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
```

- [ ] **Step 2: Rodar e confirmar que falha**

```powershell
python -m pytest tests/test_monitor_milhas.py -k "acento or normalizar" -v
```

Esperado: FALHAM os quatro que dependem da normalização — `pontuar` não casa texto sem acento, `extrair_bonus` só casa `bônus`, e `_normalizar` não existe (`AttributeError`). `test_extrair_bonus_ainda_casa_com_acento` passa desde já.

- [ ] **Step 3: Adicionar o import de `unicodedata`**

Em `monitor_milhas.py`, no bloco de imports da stdlib, mantendo a ordem alfabética:

```python
import sys
import time
import unicodedata
```

- [ ] **Step 4: Adicionar `_normalizar()` e `_KEYWORDS_NORM` logo abaixo de `KEYWORDS`**

Colocar depois do fechamento do dicionário `KEYWORDS` e antes de `SCORE_MINIMO`:

```python
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
```

- [ ] **Step 5: Fazer `pontuar()` usar a versão normalizada**

Substituir a função inteira:

```python
def pontuar(texto: str) -> tuple[int, list[str]]:
    baixo = _normalizar(texto)
    score, achados = 0, []
    for termo, peso in _KEYWORDS_NORM.items():
        if termo in baixo:
            score += peso
            if peso > 0:
                achados.append(termo)
    return score, achados
```

O `texto.lower()` sai — `_normalizar` já minusculiza.

- [ ] **Step 6: Tirar o acento do `_RE_BONUS`**

```python
_RE_BONUS = re.compile(r"(\d{2,3})\s*%\s*(?:de\s+)?bonus", re.IGNORECASE)
```

- [ ] **Step 7: Normalizar a entrada das duas funções de extração**

```python
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
```

`_RE_MILHEIRO` e `_RE_MILHEIRO_ALT` não têm acento, mas casam `R\$` maiúsculo — como `_normalizar` minusculiza, eles dependem do `re.IGNORECASE` que já carregam. Não remova essa flag.

- [ ] **Step 8: Rodar a suíte inteira**

```powershell
python -m pytest tests/test_monitor_milhas.py -v
```

Esperado: 8 passed.

- [ ] **Step 9: Commit**

```powershell
git add monitor_milhas.py tests/test_monitor_milhas.py
git commit -m @'
feat: casa palavras-chave independente de acentuacao

Blog que escreva "bonus" sem circunflexo nao casava nenhuma chave. As
chaves seguem acentuadas no fonte, legiveis; a comparacao usa uma versao
de-acentuada derivada na importacao.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
'@
```

---

### Task 3: Bypass do corte por score quando há sinal forte

Score é heurística; milheiro extraído é fato. Hoje o corte por `SCORE_MINIMO` acontece antes da extração, então um post que anuncie "milheiro por R$ 27" é descartado se o vocabulário não bater.

**Files:**
- Modify: `monitor_milhas.py` — constante `BONUS_FORTE`, função `sinal_forte()`, filtro em `varrer()`
- Modify: `tests/test_monitor_milhas.py` (acrescentar seção)

**Interfaces:**
- Consumes: `extrair_milheiro`, `extrair_bonus` da Task 2 (já normalizando internamente); `carregar_estado`/`salvar_estado` da Task 1
- Produces: `sinal_forte(milheiro: float | None, bonus_pct: int | None) -> bool`, `BONUS_FORTE: int`

- [ ] **Step 1: Escrever os testes que falham**

Os dois últimos testes montam um feed falso, então acrescente `import types` ao bloco de imports no topo de `tests/test_monitor_milhas.py`:

```python
import json
import types

import monitor_milhas as mm
```

E acrescente ao fim do arquivo:

```python
# ---------------------------------------------------------------------------
# SINAL FORTE
# ---------------------------------------------------------------------------

def test_sinal_forte_com_milheiro_dentro_do_teto():
    assert mm.sinal_forte(27.0, None) is True


def test_sinal_forte_ignora_milheiro_acima_do_teto():
    assert mm.sinal_forte(45.0, None) is False


def test_sinal_forte_com_bonus_alto():
    assert mm.sinal_forte(None, 70) is True


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

    alertas = mm.varrer()

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

    assert mm.varrer() == []
```

- [ ] **Step 2: Rodar e confirmar que falha**

```powershell
python -m pytest tests/test_monitor_milhas.py -k "sinal_forte or varrer" -v
```

Esperado: os cinco de `sinal_forte` falham com `AttributeError: module 'monitor_milhas' has no attribute 'sinal_forte'`; `test_varrer_alerta_post_de_score_baixo_que_anuncia_milheiro` falha com `assert 0 == 1` (o post é descartado pelo corte). `test_varrer_ignora_post_irrelevante` passa desde já.

- [ ] **Step 3: Adicionar a constante `BONUS_FORTE`**

Junto das outras constantes de decisão, logo abaixo de `MILHEIRO_TETO`:

```python
SCORE_MINIMO = 14          # abaixo disso, ignora
MILHEIRO_ALVO = 25.0       # R$ por 1.000 pontos Livelo — alerta URGENTE abaixo disso
MILHEIRO_TETO = 30.0       # acima disso, nunca comprar
BONUS_FORTE = 50           # % de bônus que dispensa o corte por score
```

- [ ] **Step 4: Adicionar `sinal_forte()`**

Na seção EXTRAÇÃO, logo depois de `extrair_bonus()`:

```python
def sinal_forte(milheiro: float | None, bonus_pct: int | None) -> bool:
    """Milheiro ou bônus extraído é fato; o score é palpite. Fato tem precedência."""
    if milheiro is not None and milheiro <= MILHEIRO_TETO:
        return True
    return bonus_pct is not None and bonus_pct >= BONUS_FORTE
```

- [ ] **Step 5: Extrair antes de cortar, em `varrer()`**

Substituir o trecho que hoje vai de `score, termos = pontuar(texto)` até o fim do `novos.append(...)` por:

```python
            score, termos = pontuar(texto)
            milheiro = extrair_milheiro(texto)
            bonus_pct = extrair_bonus(texto)

            # Sinal duro fura o corte: o dicionário pode estar incompleto,
            # mas um milheiro anunciado no título não deixa dúvida.
            if score < SCORE_MINIMO and not sinal_forte(milheiro, bonus_pct):
                continue

            novos.append(Alerta(
                fonte=fonte,
                titulo=titulo,
                link=entrada.get("link", ""),
                score=score,
                milheiro=milheiro,
                bonus_pct=bonus_pct,
                termos=termos,
            ))
```

- [ ] **Step 6: Rodar a suíte inteira**

```powershell
python -m pytest tests/test_monitor_milhas.py -v
```

Esperado: 15 passed.

- [ ] **Step 7: Commit**

```powershell
git add monitor_milhas.py tests/test_monitor_milhas.py
git commit -m @'
feat: milheiro ou bonus extraido dispensa o corte por score

O corte por SCORE_MINIMO rodava antes da extracao, entao um post anunciando
"milheiro por R$ 27" era descartado se o vocabulario nao batesse. Extracao
passa a vir antes, e o sinal duro fura o corte.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
'@
```

---

### Task 4: Preparar o repositório para o CI

Dependências declaradas em arquivo, fim de linha normalizado (o CI roda em Ubuntu e escreve o arquivo de estado), estado versionado e semeado para a estreia não virar enxurrada.

**Files:**
- Create: `requirements.txt`
- Create: `.gitattributes`
- Modify: `.gitignore`
- Create: `.monitor_state.json` (semeado e versionado)

**Interfaces:**
- Consumes: o script já corrigido pelas Tasks 1-3
- Produces: `requirements.txt` consumido pelo `pip install -r` da Task 5; `.monitor_state.json` versionado, consumido pelo checkout da Task 5

- [ ] **Step 1: Criar o `requirements.txt`**

```
feedparser>=6.0
requests>=2.31
```

- [ ] **Step 2: Criar o `.gitattributes`**

O Windows converte para CRLF e o runner do Actions escreve LF. Sem isso, cada execução na nuvem gera diff de fim de linha no arquivo de estado.

```
* text=auto eol=lf
```

- [ ] **Step 3: Reescrever o `.gitignore`**

Sai o `.monitor_state.json` (agora é versionado de propósito), entram o cache do pytest e o diretório de trabalho do processo de implementação:

```
.env
monitor.log
__pycache__/
.pytest_cache/
.superpowers/
```

`.superpowers/` guarda o registro de progresso e os pacotes de revisão desta implementação — é rascunho local, nunca deve ser versionado.

- [ ] **Step 4: Semear o estado sem enviar nada ao Telegram**

Estado vazio faria a estreia alertar todos os posts de todos os feeds de uma vez. Este comando popula o arquivo em modo dry-run: as variáveis são postas vazias no processo antes do import, `carregar_env()` não sobrescreve o que já está no ambiente, e `enviar_telegram()` cai no ramo dry-run com valor vazio.

```powershell
python -c "import os; os.environ['TELEGRAM_BOT_TOKEN']=''; os.environ['TELEGRAM_CHAT_ID']=''; import monitor_milhas; monitor_milhas.main()"
```

Esperado: saída com `[dry-run]` para cada alerta encontrado, ou `nada novo`. **Nenhuma mensagem deve chegar no seu Telegram** — se chegar, pare e investigue antes de seguir.

- [ ] **Step 5: Conferir que o estado foi gravado**

```powershell
python -c "import json,pathlib; d=json.loads(pathlib.Path('.monitor_state.json').read_text(encoding='utf-8')); print(len(d),'IDs'); print(d[:3])"
```

Esperado: contagem maior que zero e três URLs/IDs de exemplo.

- [ ] **Step 6: Conferir que o `.env` continua fora**

```powershell
git status --short; git check-ignore -v .env
```

Esperado: `.env` **não** aparece no status, e o `check-ignore` confirma a regra.

- [ ] **Step 7: Commit**

```powershell
git add requirements.txt .gitattributes .gitignore .monitor_state.json
git commit -m @'
chore: prepara o repo para execucao no CI

Dependencias em requirements.txt, fim de linha normalizado para LF, e o
arquivo de estado passa a ser versionado, semeado em dry-run para a
primeira execucao na nuvem nao disparar uma enxurrada de alertas.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
'@
```

---

### Task 5: Workflow do GitHub Actions

**Files:**
- Create: `.github/workflows/monitor.yml`

**Interfaces:**
- Consumes: `requirements.txt` e `.monitor_state.json` da Task 4; `monitor_milhas.py` das Tasks 1-3
- Produces: workflow `monitor-milhas`, disparável por cron e por `workflow_dispatch`. Consome os secrets `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`, criados na Task 6.

- [ ] **Step 1: Criar `.github/workflows/monitor.yml`**

```yaml
name: monitor-milhas

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:

# Escopo mínimo: só o necessário para commitar o estado de volta.
permissions:
  contents: write

# O cron do GitHub atrasa; sem isto, um run atrasado cairia em cima do
# próximo e os dois disputariam o push do estado.
concurrency:
  group: monitor-milhas
  cancel-in-progress: false

jobs:
  varrer:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip

      - name: Instalar dependências
        run: pip install -r requirements.txt

      - name: Varrer feeds e alertar
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python monitor_milhas.py

      - name: Persistir o estado
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add .monitor_state.json
          if ! git diff --staged --quiet; then
            git commit -m "estado: $(date -u '+%Y-%m-%d %H:%M UTC')"
            git push
          fi
```

- [ ] **Step 2: Validar a sintaxe do YAML antes de empurrar**

Um erro de indentação só apareceria depois do push, como workflow que não roda. O PyYAML não está instalado; instalar como ferramenta de conveniência (não entra em nenhum requirements — o runtime não usa YAML):

```powershell
python -m pip install pyyaml --quiet; python -c "import yaml,pathlib; d=yaml.safe_load(pathlib.Path('.github/workflows/monitor.yml').read_text(encoding='utf-8')); print('YAML valido, jobs:', list(d['jobs']))"
```

Esperado: `YAML valido, jobs: ['varrer']`.

- [ ] **Step 3: Conferir que os testes seguem verdes**

```powershell
python -m pytest tests/test_monitor_milhas.py -v
```

Esperado: 15 passed.

- [ ] **Step 4: Commit**

```powershell
git add .github/workflows/monitor.yml
git commit -m @'
feat: workflow do Actions rodando o monitor a cada 30 minutos

Cron 24/7 mais disparo manual. Escopo de permissao limitado a contents:write
para o push do estado, e grupo de concorrencia para run atrasado nao disputar
o push com o seguinte.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
'@
```

---

### Task 6: Publicar no GitHub e verificar de ponta a ponta

Esta task tem passos manuais — o `gh` CLI não está instalado nesta máquina.

**Files:**
- Nenhum arquivo alterado. Configuração no GitHub e push.

**Interfaces:**
- Consumes: tudo das Tasks 1-5
- Produces: repositório público com os secrets configurados e um run verde comprovando a cadeia inteira

- [ ] **Step 1: Criar o repositório no GitHub (manual)**

Em https://github.com/new:
- Nome: `monitor-milhas`
- Visibilidade: **Public**
- **Não** marcar "Add a README file", nem .gitignore, nem license — o repo local já tem histórico e qualquer arquivo inicial causaria conflito no primeiro push.

- [ ] **Step 2: Cadastrar os secrets (manual)**

Em Settings → Secrets and variables → Actions → New repository secret, dois de uma vez:

| Name | Secret |
|---|---|
| `TELEGRAM_BOT_TOKEN` | o token do @claudefolio_bot (está no `.env` local) |
| `TELEGRAM_CHAT_ID` | `611668302` |

- [ ] **Step 3: Ligar o repo local ao remoto e empurrar**

Trocar `<SEU-USUARIO>` pelo usuário real:

```powershell
git remote add origin https://github.com/<SEU-USUARIO>/monitor-milhas.git
git push -u origin main
```

- [ ] **Step 4: Conferir que o `.env` não subiu**

```powershell
git ls-files | Select-String -Pattern "^\.env$"
```

Esperado: **nenhuma saída**. Se o `.env` aparecer, revogue o token no @BotFather imediatamente.

- [ ] **Step 5: Disparar o workflow manualmente**

No GitHub: aba Actions → workflow `monitor-milhas` → botão "Run workflow" → branch `main` → Run workflow.

- [ ] **Step 6: Verificar o run**

O run verde prova as três coisas de uma vez:
1. Passo "Varrer feeds e alertar" concluiu sem erro → os secrets chegaram.
2. Se houve alerta, a mensagem chegou no Telegram.
3. Passo "Persistir o estado" → ou fez commit novo, ou não havia mudança.

Como o estado foi semeado na Task 4, o esperado neste primeiro run é `nada novo` ou pouquíssimos alertas — não uma enxurrada.

- [ ] **Step 7: Confirmar o agendamento**

Na aba Actions, conferir que o próximo run agendado aparece sozinho dentro de ~30 min. Atraso de 10-30 min é normal e esperado.

---

## Notas operacionais pós-implementação

- Workflow agendado é desativado após 60 dias sem atividade no repositório. O GitHub avisa por email; basta reativar pela aba Actions ou empurrar qualquer commit.
- Push feito com `GITHUB_TOKEN` não dispara novo workflow — não há risco de loop entre o commit de estado e o cron.
- Para ajustar pesos de `KEYWORDS` ou o `SCORE_MINIMO`, editar, commitar e empurrar: o próximo run já usa os valores novos.
- Para silenciar de madrugada, use o silenciamento por horário do próprio Telegram — não mexa no cron.
