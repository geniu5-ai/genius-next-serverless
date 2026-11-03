# Cofre do Standby: ideias, decisões e alternativas avaliadas

Registro da pesquisa de 03/nov/2026 e do que virou código. O funcionamento e os comandos estão em
[`OPERACAO.md`](OPERACAO.md) ("Cofre"); aqui ficam o **porquê**, o estado de cada ideia e o que foi
descartado. Contexto: o Standby é um app pequeno; as ideias valem como ensaio de um padrão que pode
ir para o Genius Next (`runbooks/RUNBOOK-SEGREDOS-NO-PG.md` no repositório do Next).

## As seis ideias

| # | Ideia | Estado | Onde |
|---|---|---|---|
| 1 | Ler só por função, com registro de acesso | **feito** | `scripts/vault_schema.sql`: `vault_get` registra em `access_log`; `vault_rw` não tem acesso às tabelas |
| 2 | Histórico e "apagar sem perder" | **feito** | `secret_versions` (somente inserção); `vault_delete` guarda a última versão; `history` / `restore` |
| 3 | `run NOME... -- comando` | **feito** | `vault.py run`: segredo só no ambiente do processo filho, nada impresso |
| 4 | Backup cifrado (`export` / `import`) | **feito** | JSON só com texto cifrado + sha256, modo 0600; `import` confere checksum e se a chave abre tudo |
| 5 | Chave mestra em duas partes | **feito** | formato `aead2` (HKDF sobre `KEY` + `0x00` + `KEY2`); `STANDBY_VAULT_KEY2` |
| 6 | Prazo de rotação | **feito** | coluna `rotate_by`; `vault.py expire NOME DIAS`, `vault.py due [DIAS]` |

### 1. Leitura por função, com registro
- **Problema:** com `SELECT` direto na tabela não há como saber quem leu o quê.
- **Desenho:** a role do cofre só tem `EXECUTE` em funções `SECURITY DEFINER`, com `search_path` fixo
  e `EXECUTE` revogado de `PUBLIC` (recomendação de [Cybertec](https://cybertec-postgresql.com/en/?p=8278)
  contra o abuso de `SECURITY DEFINER`). Cada leitura grava uma linha em `access_log`.
- **Limites:** o rótulo de quem chamou vem de `application_name`, informado pelo cliente. O dono do
  banco ainda lê tudo (cifrado) e pode desligar os gatilhos. O log é rastro, não cofre-forte.

### 2. Histórico
- **Problema:** o GCM detecta linha adulterada, mas não linha apagada nem devolvida a uma versão
  antiga.
- **Desenho:** toda escrita e todo `delete` gravam uma versão (com o texto cifrado) em tabela
  somente de inserção; gatilhos recusam `UPDATE`, `DELETE` e `TRUNCATE` (testado: barram até o dono).
- **Limite:** o dono do banco pode desligar o gatilho. Por isso existe o backup (ideia 4).

### 3. `run`
- **Problema:** `vault.py get` imprime o valor; em terminal compartilhado ou log ele vaza.
- **Desenho:** `vault.py run RENDER_API_KEY -- ./script.sh` decifra e põe no ambiente do filho. É o
  mesmo padrão de ferramentas como a Doppler (`doppler run`).

### 4. Backup cifrado
- **Problema:** apagar a tabela, o schema ou o projeto do Neon perde o cofre.
- **Desenho:** `export` grava as linhas cifradas num JSON com checksum e **recusa gerar um backup que
  a chave atual não abre**; `import` confere o checksum e a chave antes de gravar.
- **Regra:** o arquivo vai para fora do banco (gerenciador de senhas, Drive ou o bucket do R2). Não
  consegui confirmar a janela de restauração do Neon no plano free; confira no painel.

### 5. Chave mestra em duas partes (a mais valiosa)
- **Problema:** a chave mestra é ponto único de falha; quem tem a URL do cofre e a chave lê tudo.
- **Desenho:** `STANDBY_VAULT_KEY2` opcional. Com ela, o `put` grava `aead2` e a chave de 256 bits sai
  de HKDF sobre as **duas** partes. Linhas `aead1` continuam abrindo só com a primeira parte, então a
  proteção reforçada é **por entrada**: hoje `RENDER_API_KEY` e `NEON_API_KEY` (as de administração)
  estão em `aead2`; o resto, em `aead1`.
- **Modelo de uso:** a parte 1 fica no ambiente da sessão; a parte 2 fica **fora** dele (gerenciador
  de senhas) e só é definida quando a sessão precisa de uma entrada `aead2`. Se as duas partes
  estiverem sempre no mesmo ambiente, o ganho é pequeno; o valor está em guardá-las em lugares
  diferentes.
- **Testado:** sem a parte 2 a entrada não abre (mensagem clara), com parte 2 errada não abre, com
  partes trocadas de ordem não abre, e `aead1` abre mesmo com `KEY2` definida. `rotate` de ida e
  volta (uma parte → duas partes → uma parte) preservou todos os valores.

### 6. Prazo de rotação
- **Problema:** chaves que vazam em conversa ou ficam velhas não têm quem as lembre.
- **Desenho:** coluna `rotate_by`. `expire NOME DIAS` define o prazo; **um `put` com valor novo zera
  o prazo** (valor novo = rotação feita); `due [DIAS]` lista o vencido, o que vence em `DIAS` (padrão
  14) e o que está sem prazo e com mais de 90 dias. Hoje as oito entradas têm prazo de 90 dias.
- **Não faz:** avisar sozinho. Possível próximo passo: um passo no `wake.yml` (ou outro workflow)
  que roda `due` e abre uma issue.

## Avaliado e deixado de fora (por enquanto)

| Opção | Por que não agora |
|---|---|
| **Envelope encryption** (chave por segredo, embrulhada por uma chave mestra) | Ganho: rotação da chave mestra sem recifrar tudo ([referência](https://archman.dev/docs/security-architecture/data-protection/encryption-at-rest-envelope-encryption)). Com 8 segredos o `rotate` atual leva segundos. Só vale uma coluna `key_id` quando houver mais de uma chave em uso |
| **AES-GCM-SIV** (`AESGCMSIV` na `cryptography`) | Resistente a nonce repetido. Com nonce aleatório e poucas escritas o risco do GCM atual é desprezível. Os limites numéricos que lembro (2^32 mensagens por chave no GCM, 2^48 no GCM-SIV) **não foram verificados** nas fontes; confirme no RFC 8452 antes de citar |
| **pgsodium / criptografia transparente de coluna** | O próprio Supabase [desaconselha](https://supabase.com/docs/guides/database/column-encryption) (em descontinuação, alta complexidade e risco de configuração errada). `pgcrypto` cifra no servidor, e o valor e a chave passariam por ele |
| **OpenBao** | Alto custo operacional segundo as comparações; é um serviço novo para um app pequeno |
| **Infisical** | Novo app e novo painel; sem *transit engine* |
| **SOPS + age** | Bom para arquivos no git, mas sem log de leitura nem rotação embutida; não serve como cofre consultável |
| **Bitwarden Secrets Manager** | Era a opção inicial. A fonte que achei sobre o plano gratuito não tem data (2 usuários, 3 contas de máquina): confirme na página de preços. O chairman preferiu o Postgres que já usamos |

Fontes: comparações de [Infisical](https://infisical.com/blog/open-source-secrets-management-devops.md)
(fornecedor, leia com cautela), [Neon: extensões](https://neon.com/docs/extensions/pg-extensions),
[Supabase: pgsodium](https://supabase.com/docs/guides/database/column-encryption). A busca **não**
confirmou pgaudit no Neon nem a janela de restauração por plano.

## Se quiser ir além
- Avisar de vencimento por issue do GitHub (workflow que roda `due`).
- Coluna `key_id` e envelope encryption, se o número de segredos crescer.
- Trocar o dono do banco usado pelo app por uma role sem `neon_superuser` (hoje o `DATABASE_URL` do
  app é o dono): reduziria o que um vazamento do app alcança, inclusive no cofre.
