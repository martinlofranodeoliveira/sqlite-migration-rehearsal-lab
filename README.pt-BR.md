# SQLite Migration Rehearsal Lab

**Versão de desenvolvimento 0.1.0 — não lançada.** Software original MIT da [Lofrano Tecnologia](https://lofranotecnologia.com.br).

Ensaia SQL confiável em uma cópia SQLite consistente e descartável: verificações iniciais → up → invariantes escalares explícitas → commit → down → comparação com a origem lógica → reaplicação de up → comparação entre os dois estados up. A conexão da origem é somente leitura. Não implanta migrações nem prova equivalência semântica geral de SQL.

[English](README.md) · [Contrato](docs/contract.md) · [Projeto](docs/design.md)

## Executar sem instalar

Requer Python 3.13+ com SQLite 3.51.3+ realmente vinculado. O Dockerfile fornecido compila o runtime de referência qualificado. Execução, testes e demonstração usam somente a biblioteca padrão, sem rede, modelos, APIs, credenciais ou configuração no diretório pessoal. No diretório do projeto:

```sh
python3 -m unittest discover -s tests -v
python3 -m sqlite_migration_rehearsal demo
python3 -m sqlite_migration_rehearsal demo --json
python3 -m sqlite_migration_rehearsal validate-manifest --manifest fixtures/manifests/users_add_status_reversible.json
python3 -m sqlite_migration_rehearsal run --seed users_basic --manifest fixtures/manifests/users_add_status_reversible.json --json
```

Saída humana da demonstração:

```text
SQLite Migration Rehearsal Lab 0.1.0 (development)
Expectations: PASS
Source bytes preserved: True
Owned workspace removed: True
reversible_up_down_up: passed
lossy_down_drops_row: RESTORATION_MISMATCH
failing_up_after_update_rolls_back: INVARIANT_MISMATCH
sql_failure_after_update_rolls_back: SQL_EXECUTION_ERROR
```

JSON inclui fases, hashes lógicos, resumos sem células, contagens de diferenças e verificação do rollback. Contagens de instruções e hashes físicos podem variar entre versões do SQLite; os relatórios não incluem horários nem caminhos temporários.

## Instalação opcional

A preparação de dependências de construção é separada dos testes offline. Usa ferramentas reais em ambiente isolado e pode acessar o índice de pacotes:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m sqlite_migration_rehearsal demo
```

Setuptools 80.9.0 é dependência somente de construção. Não há dependências em tempo de execução. O comando instalado também se chama `sqlite-migration-rehearsal`.

## Origem explícita e API

Selecione um arquivo SQLite local confiável, finito, em diretórios-pai confiáveis:

```sh
python3 -m sqlite_migration_rehearsal run --baseline ./synthetic.sqlite3 --manifest fixtures/manifests/users_add_status_reversible.json --max-vm-steps 50000
```

```python
from sqlite_migration_rehearsal import load_manifest_file, rehearse_migration

manifest = load_manifest_file("fixtures/manifests/users_add_status_reversible.json")
result = rehearse_migration("synthetic.sqlite3", manifest)
print(result.status)
print(result.to_report_dict()["restoration_diff"])
```

O seed contém apenas dados sintéticos: `(1, 'Ada')` e `(2, 'Lin')`. Seu criador nunca sobrescreve um arquivo existente. O parser lança `RehearsalError` sanitizado; o motor devolve `RehearsalResult` com falha para erros esperados de entrada, SQLite e sistema de arquivos. Configurações inválidas de `RehearsalLimits` geram `ValueError`.

## Decisões de projeto

Comparar nomes de tabelas não detecta perda de dados. O down propositalmente incorreto omite Lin, mantendo o esquema, e falha na restauração. Outro teste detecta a exclusão em cascata de um filho causada pela reconstrução da tabela-pai. O exemplo válido remove a coluna sem excluir a tabela-pai. Uma falha SQL após UPDATE e uma invariante inesperada exercitam rollback explícito; rollback não é a migração down escrita pelo desenvolvedor.

Comparam-se SQL armazenado exato, metadados estruturados de colunas/PK/FK/índices e linhas tipadas ordenadas pela chave. Não se normaliza espaço em SQL: definições semanticamente equivalentes podem divergir, de forma conservadora.

## Limitações e segurança

Padrões: manifesto de 1 MiB, origem mais arquivos auxiliares de 16 MiB, 32 tabelas, 10.000 linhas totais, 64 instruções **por direção**, 16.384 bytes UTF-8 por instrução, 32 invariantes por lista e 50.000 passos de VM por lote de migração, invariante ou inspeção. O [contrato](docs/contract.md) detalha limites adicionais de esquema, células, registros, serialização e backup.

Suporta tabelas rowid comuns com chave primária explícita, efetivamente única e não nula; valores INTEGER/TEXT/NULL/BLOB limitados. Rejeita REAL, views, triggers, tabelas virtuais, colunas geradas, WITHOUT ROWID, índices de expressão, collations personalizadas e tabelas internas. Funções SQL usam lista de permissão pequena.

SQL confiável **não é sandbox**. Limites de passos não garantem limites de memória ou tempo; funções SQLite e acesso a disco podem custar caro. Use limites externos de processo. O backup tem limites independentes de páginas e tentativas/callbacks. Não altere caminhos concorrentemente. Escritores externos não são impedidos; SHA-256 do arquivo principal comprova preservação somente em fixtures quiescentes, não identifica todo o estado WAL.

Relatórios/CLI não exibem SQL nem células, mas preservam identificadores e hashes: escapar caracteres não anonimiza dados. Snapshots da API contêm dados e SQL fornecidos pelo chamador. Falha de limpeza é informada e pode deixar somente o workspace temporário próprio para limpeza pelo host. Nenhum diretório arbitrário do chamador é removido.

Códigos de saída: 0 sucesso esperado (inclusive falhas esperadas da demo); 1 falha do ensaio/demo/limite de saída; 2 argumentos, manifesto, configuração ou preparação externa inválidos. Sem telemetria ou implantação remota.

Relatos de segurança: [reporte privado do GitHub](https://github.com/martinlofranodeoliveira/sqlite-migration-rehearsal-lab/security/advisories/new), quando disponível. Caso contrário, solicite um canal privado pelo [site oficial](https://lofranotecnologia.com.br).

## Runtime SQLite

Use a [compilação com checksum e qualificação por SQL](docs/sqlite-runtime.md). A aplicação exige SQLite 3.51.3+ realmente vinculado; a imagem fornecida usa 3.54.1. Compile com `docker build -t sqlite-migration-rehearsal-lab-sqlite .` e execute os comandos documentados de teste/demo com `docker run --rm --network none sqlite-migration-rehearsal-lab-sqlite <argv>`. Instalações nativas devem fornecer SQLite na faixa suportada.
