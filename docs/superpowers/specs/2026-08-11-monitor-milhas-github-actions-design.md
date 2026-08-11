# Monitor de milhas na nuvem via GitHub Actions

Data: 2026-08-11
Status: aprovado, pronto para plano de implementação

## Problema

`monitor_milhas.py` só roda quando a máquina está ligada. Promoção de pontos
costuma sair à noite ou no fim de semana e durar 48h — notebook fechado
significa promoção perdida. O script precisa rodar sozinho, 24/7, sem servidor.

Junto disso, dois defeitos de filtragem ficam mais caros na nuvem do que local:
o dicionário de palavras-chave não tolera variação de acentuação, e um post que
anuncia o milheiro explicitamente pode ser descartado por não bater vocabulário.

## Decisões

| Questão | Decisão | Motivo |
|---|---|---|
| Onde roda | GitHub Actions, cron | Grátis, sem servidor, o usuário já usa git |
| Estado entre execuções | Commitado de volta no repo | Único método durável; cache do Actions é despejado em 7 dias e gera lote de alertas repetidos |
| Visibilidade do repo | Público | Minutos de Actions ilimitados; nada sensível no repo |
| Frequência | `*/30 * * * *`, 24/7 | Nunca perde promo noturna; cron de uma linha, sem fuso |
| Normalização de acento | Chaves normalizadas no carregamento | Mantém `KEYWORDS` legível com acento no fonte |

## Arquitetura

O script roda idêntico local e na nuvem. As duas únicas diferenças:

- **Credenciais**: `.env` local (ignorado pelo git), GitHub Secrets na nuvem.
  `carregar_env()` já resolve — sai imediatamente se não achar `.env`, e nunca
  sobrescreve variável já presente no ambiente, então os Secrets entram por cima.
- **Estado**: local o arquivo só existe no disco; na nuvem ele é versionado.

Fluxo de uma execução na nuvem:

```
cron (*/30)
  -> checkout            traz o .monitor_state.json versionado
  -> setup-python 3.12   + pip install -r requirements.txt
  -> python monitor_milhas.py
       le TELEGRAM_* dos Secrets
       varre os 4 feeds, pontua, extrai milheiro/bonus
       envia os alertas ao Telegram
       reescreve .monitor_state.json
  -> se o estado mudou: commit + push de volta
```

## Componentes

### 1. `.github/workflows/monitor.yml` (novo)

- Gatilhos: `schedule` com `*/30 * * * *` e `workflow_dispatch` (disparo manual).
- `permissions: contents: write` — escopo mínimo necessário para o push do estado.
- `concurrency` com grupo fixo e `cancel-in-progress: false` — se um run atrasar
  e cair em cima do próximo, eles enfileiram em vez de disputar o push.
- Passo final commita `.monitor_state.json` apenas se houve mudança
  (`git diff --staged --quiet ||`), com autor `github-actions[bot]`.
- Push feito com `GITHUB_TOKEN` não dispara novo workflow: não há loop.

### 2. `requirements.txt` (novo)

`feedparser` e `requests`. Permite `cache: pip` no `setup-python` e vira a fonte
única de verdade das dependências, hoje só documentadas na docstring.

### 3. `.gitignore` (alterado)

Remover a linha `.monitor_state.json` — o arquivo passa a ser versionado de
propósito. `.env` continua ignorado e nunca sobe.

### 4. `monitor_milhas.py` (três alterações)

**4.1 — Truncamento do estado (correção de bug).**
`salvar_estado()` faz `list(vistos)[-MAX_STATE_ENTRIES:]` sobre um `set`. A ordem
de iteração de um set em Python é arbitrária (função do hash), então o corte não
guarda as 2.000 entradas mais recentes — guarda 2.000 quaisquer. Local, com o
arquivo sempre intacto, o defeito é invisível. Na nuvem o dedup é a única coisa
segurando o spam: passando de 2.000 entradas (estimativa de 2-3 meses a 20-40
posts/dia somando os 4 blogs), IDs recentes começam a ser descartados ao acaso e
posts antigos voltam a alertar.

Correção: preservar ordem de inserção. `carregar_estado()` devolve um
`dict[str, None]` (dict preserva ordem de inserção) usado como conjunto
ordenado; `salvar_estado()` corta pelo começo, descartando os mais antigos. Em
`varrer()`, `vistos.add(uid)` vira `vistos[uid] = None` — o teste `uid in vistos`
não muda. O formato do JSON em disco continua sendo uma lista de strings,
compatível com qualquer estado já existente.

**4.2 — Normalização de acento.**
Se um blog escrever "bonus" sem circunflexo, nenhuma chave acentuada casa.
Adicionar:

```python
def _normalizar(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower())
        if unicodedata.category(c) != "Mn"
    )
```

`KEYWORDS` continua escrito com acento no fonte (legível, e é o que o usuário
acabou de ajustar). Um `_KEYWORDS_NORM` derivado é construído uma vez na
importação, de-acentuando as chaves. `pontuar()` compara texto normalizado
contra chaves normalizadas.

O mesmo vale para a extração: `_RE_BONUS` casa `bônus` com acento e falharia
pela mesma razão. O padrão vira `bonus` e passa a rodar sobre texto normalizado.
`_RE_MILHEIRO` e `_RE_MILHEIRO_ALT` não têm acento; como `_normalizar` também
minusculiza, o `R\$` desses padrões depende do `re.IGNORECASE` que eles já
carregam.

Onde normalizar: cada uma das três funções públicas (`pontuar`,
`extrair_milheiro`, `extrair_bonus`) chama `_normalizar` sobre o próprio
argumento. `_normalizar` é idempotente, então normalizar de novo não custa
correção — e evita o contrato implícito de "esta função só aceita texto já
normalizado", que seria fácil de violar em qualquer chamada futura.
`pontuar()` deixa de fazer `texto.lower()`, já embutido em `_normalizar`.

Consequência visível: os termos reportados em `Alerta.termos` (e portanto na
mensagem do Telegram) passam a aparecer sem acento, porque vêm das chaves
normalizadas. Aceito — é o rodapé de diagnóstico do alerta, não o conteúdo.

**4.3 — Bypass por sinal forte.**
Score é heurística; milheiro extraído é fato. Hoje `varrer()` aplica o corte por
`SCORE_MINIMO` antes de extrair, então um post cujo título diga "milheiro por
R$ 27" é descartado se o vocabulário não bater. Inverter a ordem: extrair
primeiro, e deixar o sinal duro furar o corte.

```python
mi = extrair_milheiro(texto)
bo = extrair_bonus(texto)
forte = (mi is not None and mi <= MILHEIRO_TETO) or (bo is not None and bo >= 50)
if score < SCORE_MINIMO and not forte:
    continue
```

Tradeoff aceito: o sinal forte é absoluto, então um post majoritariamente ruído
(score negativo) que cite um milheiro dentro do teto vai alertar. É raro, e é
exatamente o caso que o bypass existe para cobrir — dicionário incompleto. O
alerta mostra o score, então a origem fica óbvia na mensagem.

Efeito colateral: um `Alerta` pode agora ter score abaixo de `SCORE_MINIMO`.
Nada quebra — `urgente` já decide por milheiro e bônus antes de olhar o score.

## Semeadura da primeira execução

Estado vazio significa que todo post de todos os feeds é novo — a estreia
mandaria dezenas de alertas de uma vez. Antes do primeiro push, rodar local em
dry-run com `TELEGRAM_BOT_TOKEN=""` e `TELEGRAM_CHAT_ID=""`: variável de
ambiente tem precedência sobre o `.env`, e `enviar_telegram()` cai no ramo
dry-run com valor vazio, imprimindo em vez de enviar. O estado é populado, e
commitamos o arquivo já preenchido. A nuvem começa em silêncio.

## Segurança

- `.env` está no `.gitignore` desde antes do `git init`; conferir com
  `git status` que ele não aparece antes do primeiro commit.
- Token e chat_id vão em Settings → Secrets and variables → Actions. Secrets
  são criptografados e mascarados no log mesmo em repo público.
- O repo público expõe o código, os pesos de palavra-chave e as URLs dos posts
  no arquivo de estado. Nada disso é sensível.
- O workflow pede só `contents: write`.

## Riscos e limites operacionais

| Risco | Mitigação |
|---|---|
| Cron do GitHub atrasa 10-30 min em pico | Aceito — promoções duram horas ou dias |
| Workflow agendado é desabilitado após 60 dias sem atividade no repo | GitHub avisa por email; `workflow_dispatch` reativa |
| Um feed sai do ar | Já tratado: `try/except` por feed, os outros seguem |
| Falha de execução | Actions marca vermelho e o GitHub manda email |

## Verificação

1. Rodar o script local após as alterações — dry-run, sem enviar nada, conferindo
   que ele varre os 4 feeds e escreve o estado.
2. Teste dos pontos alterados: normalização casando "bonus" sem acento, bypass
   deixando passar um texto de score baixo com milheiro citado, e truncamento
   preservando as entradas mais recentes.
3. `git status` antes do primeiro commit: `.env` fora.
4. Disparo manual por `workflow_dispatch`. O run verde prova de uma vez que os
   secrets chegaram, que o Telegram recebeu e que o push do estado funcionou.

## Fora de escopo

- Instalar o `gh` CLI (ausente na máquina). A criação do repo no GitHub é manual,
  pelo site, salvo pedido em contrário.
- Ajuste de pesos, novos feeds, e o formato da mensagem do Telegram.
- Deploy em VPS.
