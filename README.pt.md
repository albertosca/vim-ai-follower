# vim-ai-follower

*[English version](README.md)*

Acompanhe o Claude Code editando arquivos **ao vivo, dentro de um Vim de
verdade** — sem migrar de editor, sem GUI. Um painel tmux dedicado (ou um Neovim
já aberto) espelha cada arquivo que o Claude Code lê ou escreve, animando cada
mudança linha a linha, como se o Claude estivesse digitando no seu próprio
editor.

Instala uma vez, conecta nos hooks globais do Claude Code e funciona em qualquer
projeto onde o `claude` rode dentro do tmux.

## Como funciona

`claude-follow` é uma única CLI em Python que é ao mesmo tempo a superfície de
controle (`start`/`stop`/`status`, pausa/interrupção, velocidade, toggle) e o
handler dos hooks do Claude Code. A cada `Edit`/`MultiEdit`/`Write`, um hook
`PreToolUse` guarda o conteúdo antigo do arquivo e um hook `PostToolUse` faz o
diff contra o conteúdo novo e reproduz a mudança no follower via
`tmux send-keys`. A cada `Read`, o follower navega até aquele arquivo (e linha).

O buffer do follower fica somente-leitura entre as animações, então uma tecla
acidental nunca corrompe o que você está assistindo; a animação destrava em
volta de si mesma e trava de novo (com um resync silencioso do disco) quando
termina.

## Requisitos

- tmux, com o `claude` rodando dentro de uma sessão tmux
- Vim (backend padrão) ou Neovim com um socket RPC (backend `nvim_rpc`)
- Python 3.11+

## Instalação

```sh
pip install -e .          # a partir de um clone; instala o script `claude-follow`
```

### Conectar os hooks

Adicione estas entradas em `~/.claude/settings.json` para que o Claude Code
invoque a CLI (use o caminho absoluto do `claude-follow` instalado no seu
ambiente):

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Edit|MultiEdit|Write",
        "hooks": [{ "type": "command", "command": "claude-follow hook pre" }] }
    ],
    "PostToolUse": [
      { "matcher": "Edit|MultiEdit|Write",
        "hooks": [{ "type": "command", "command": "claude-follow hook post" }] },
      { "matcher": "Read",
        "hooks": [{ "type": "command", "command": "claude-follow hook post" }] }
    ]
  }
}
```

Os hooks nunca bloqueiam nem falham uma chamada de ferramenta — todo caminho sai
com `0`, e problemas vão para `~/.cache/claude-vim-follower/hook.log`, não para o
Claude.

## Uso

De um painel dentro da sessão tmux onde o `claude` roda:

```sh
claude-follow start      # abre um painel follower (ou adota um Vim existente)
claude-follow status     # mostra o follower ativo e o arquivo em exibição
claude-follow stop       # desmonta o follower e remove os keybindings
```

`start` aceita:

- `--backend {tmux,nvim_rpc}` — padrão `tmux`.
- `--on-failure {silent,reopen}` — o que fazer se o painel do follower morrer no meio da sessão.
- `--speed {instant,muito_rapido,rapido,normal,lento}` — ritmo inicial da animação.

Com `open_policy` em `always` ou `code` (veja abaixo), você nem precisa do
`start`: a primeira edição correspondente abre o follower automaticamente.

## Configuração

JSON opcional em `~/.config/claude-vim-follower/config.json`. Toda chave tem um
padrão, e um valor inválido cai silenciosamente para ele.

| Chave           | Valores                                                   | Padrão     | Significado                                                                              |
| --------------- | --------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------- |
| `open_policy`   | `always`, `code`, `manual`                                | `manual`   | `manual`: só o `start` abre follower. `code`: auto-abre em edições/leituras de arquivos de código; arquivos não-código também são ignorados (nunca animados) mesmo com um follower iniciado manualmente. `always`: auto-abre em qualquer arquivo. |
| `adopt_existing`| `true`, `false`                                           | `false`    | Ao abrir automaticamente (ou no `start`), reutiliza um Vim já rodando na janela em vez de dividir um novo painel. |
| `max_tabs`      | inteiro ≥ 1                                                | `5`        | Quantas abas de arquivo o follower mantém. A aba menos recentemente usada é fechada ao passar do limite. |
| `on_failure`    | `silent`, `reopen`                                        | `silent`   | Se o painel do follower morrer, `reopen` redivide um novo a partir de onde começou; `silent` apenas para de seguir. |
| `speed`         | `instant`, `muito_rapido`, `rapido`, `normal`, `lento`    | `rapido`   | Ritmo da animação (segundos por fronteira de linha: `0`, `0.01`, `0.03`, `0.08`, `0.15`). |

## Keybindings

`start` (e o auto-open) registram keybindings de **prefixo** do tmux, restaurados
no `stop`:

| Tecla         | Comando      | Efeito                                                                     |
| ------------- | ------------ | -------------------------------------------------------------------------- |
| `prefix` `P`  | pause        | Pausa uma animação em andamento; aperte de novo para retomar.              |
| `prefix` `S`  | interrupt    | Interrompe: entrega o buffer para você assumir e salvar sua própria versão. Aperte de novo durante o hand-off para descartar suas edições e retomar a do Claude. |
| `prefix` `+`  | speed-up     | Uma marcha mais rápida (satura no `instant`).                          |
| `prefix` `_`  | speed-down   | Uma marcha mais lenta (satura no `lento`).                           |
| `prefix` `F`  | toggle       | Muta/desmuta o follower.                                                   |

Os keybindings são **globais no servidor** tmux: rodar dois followers em duas
sessões tmux ao mesmo tempo não é suportado (o primeiro `stop` derruba as teclas
para os dois).

## Detalhes de comportamento

### Abas multi-arquivo

Cada arquivo que o Claude toca ganha sua própria aba no Vim. Editar um arquivo já
aberto navega de volta para a aba dele primeiro (por nome, então sobrevive a você
fechar ou reordenar abas), anima ali e volta — nunca digita na aba que
por acaso estiver ativa. A lista de abas é ordenada por uso recente; a aba menos
recentemente usada é fechada ao ultrapassar `max_tabs`. O arquivo sendo animado
ou entregue nunca é o despejado.

### Velocidade ao vivo

`prefix` `+` / `prefix` `_` releem o ritmo na hora: uma animação em andamento
acelera ou desacelera na **próxima fronteira de linha**, não só na animação
seguinte. A escala satura nas duas pontas; o popup sinaliza os limites
(`lento (slowest)`, `instant (fastest)`).

### Pausa e interrupção

- **Pausa** (`P`) segura o turno do Claude no lugar até você retomar — a animação
  termina visualmente antes de o Claude continuar. Se o processo do hook for
  morto (ex: timeout do hook) durante a pausa, um crash-fallback deixa o teclado
  terminar a animação.
- **Interrupção** (`S`) entrega o buffer para você: edite e dê `:w` na sua
  versão, e o Claude é avisado de que você assumiu (ele relê a sua versão do
  disco em vez de restaurar a dele). Apertar `S` de novo durante o hand-off
  descarta suas edições não salvas e retoma seguindo o arquivo que o Claude
  escreveu.

### Toggle de mute

`prefix` `F` muta o follower: edições seguintes são ignoradas e o painel de
origem é dado zoom para o follower sair do caminho. Apertar `F` de novo desmuta,
tira o zoom e força a próxima edição a **ressincronizar com uma redigitação
completa** — o arquivo mudou no disco enquanto estava mutado, então animar um
diff contra o buffer velho produziria lixo. As abas existentes continuam abertas
para leitura.

### Adotar um Vim existente (opt-in)

Com `adopt_existing: true`, em vez de dividir um novo painel o follower dirige um
Vim que você já tem aberto na mesma janela tmux. Como é o **seu** editor:

- A adoção é estritamente opt-in.
- A aba em que você estava nunca é renomeada por cima — um arquivo novo sempre
  abre na própria aba.
- No `stop`, a adoção nunca mata o seu Vim; só fecha as abas que ela abriu.
- **Disciplina:** pause (`P`) antes de navegar durante uma animação. Uma animação
  adotada dirige o seu cursor ao vivo, e digitar ou trocar de aba no meio da
  animação pode se misturar com as teclas injetadas.
- Risco residual: o follower não distingue as suas teclas das dele no nível do
  tty, então uma edição mal cronometrada durante uma animação não pausada ainda
  pode cair no lugar errado. O lock somente-leitura protege o buffer entre
  animações, não durante uma que você interrompa digitando.

## Backends

- **`tmux`** (padrão): dirige um Vim não modificado num painel tmux via
  `send-keys`. Todo o comportamento multi-arquivo baseado em abas acima é
  exclusivo do tmux.
- **`nvim_rpc`**: fala com um Neovim em execução via msgpack-RPC (o mesmo buffer
  que você está olhando). Ele não tem abas — troca de buffer no lugar — então o
  rastreio de abas por arquivo é um no-op ali. O lado Neovim precisa expor um
  socket via `vim.fn.serverstart()` no caminho que o `claude-follow` espera
  (impresso pelo `start --backend nvim_rpc`).

## Limitações

- Um follower por sessão tmux (o estado é indexado pelo id da sessão tmux).
- Dois processos `claude` na mesma sessão tmux compartilhariam, e poderiam
  competir por, um único follower.
- Arquivos binários são navegados, não animados.
- Arquivos muito grandes degradam para um paste em bloco quando a animação
  ficaria longa demais, em vez de ritmar tecla a tecla.
