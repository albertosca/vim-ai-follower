# vim-ai-follower

🇺🇸 [English](README.md) · 🇧🇷 [Português](README.pt.md)

[![CI](https://github.com/albertosca/vim-ai-follower/actions/workflows/ci.yml/badge.svg)](https://github.com/albertosca/vim-ai-follower/actions/workflows/ci.yml)
[![Lint](https://github.com/albertosca/vim-ai-follower/actions/workflows/lint.yml/badge.svg)](https://github.com/albertosca/vim-ai-follower/actions/workflows/lint.yml)
[![Coverage: 99% unit · 100% full](https://img.shields.io/badge/coverage-99%25%20unit%20%C2%B7%20100%25%20full-brightgreen.svg)](https://github.com/albertosca/vim-ai-follower/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2.svg)](https://code.claude.com/docs/en/plugins)

Acompanhe o Claude Code editando arquivos **ao vivo, dentro de um Vim de
verdade** — sem migrar de editor, sem GUI. Um painel tmux dedicado (ou um Neovim
já aberto) espelha cada arquivo que o Claude Code lê ou escreve, animando cada
mudança linha a linha, como se o Claude estivesse digitando no seu próprio
editor.

Instala como um plugin do Claude Code que conecta os próprios hooks, e funciona
em qualquer projeto onde o `claude` rode dentro do tmux.

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
- Vim (backend padrão `tmux`) ou Neovim ≥ 0.10 (o backend `nvim` de primeira classe)
- Python 3.11+

## Instalação

**Como plugin do Claude Code (recomendado).** O plugin declara os próprios
hooks, então não há `settings.json` para editar:

```
/plugin marketplace add albertosca/vim-ai-follower
/plugin install vim-ai-follower
```

O backend `tmux` padrão **não precisa de `pip`** — o plugin embute a CLI
stdlib-only e a roda no lugar. Para o backend `nvim`, instale também o pynvim no
`python3` do seu `PATH`:

```sh
pip install pynvim
```

Os hooks nunca bloqueiam nem falham uma chamada de ferramenta — todo caminho sai
com `0`, e problemas vão para `~/.cache/claude-vim-follower/hook.log`, não para o
Claude.

### Instalação manual / desenvolvimento

A partir de um clone — para desenvolvimento, ou se preferir não usar o plugin:

```sh
pip install -e .          # instala o script `claude-follow`
pip install -e '.[nvim]'  # adiciona o extra pynvim para o backend nvim
```

Depois conecte os hooks à mão: adicione em `~/.claude/settings.json` (use o
caminho absoluto do `claude-follow` instalado):

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

## Uso

**Se você instalou o plugin**, controle-o pelos slash commands, de um painel dentro da sessão tmux onde o `claude` roda: `/vim-ai-follower:start`, `/vim-ai-follower:status`, `/vim-ai-follower:stop`, `/vim-ai-follower:toggle`. O `/vim-ai-follower:start` repassa os argumentos direto para o `claude-follow start` (flags abaixo), ex.: `/vim-ai-follower:start --backend nvim --speed lento`. O wrapper `claude-follow` embutido de propósito não fica no `PATH` do seu shell — ele só roda a partir da própria ferramenta Bash do Claude Code e dos keybindings do tmux abaixo (veja o comentário de cabeçalho em `bin/claude-follow`). Se você também quiser rodar o `claude-follow` direto no seu shell, crie um symlink do wrapper embutido para qualquer diretório no seu `PATH` (ex.: `~/.local/bin`) — o diretório de versão varia conforme o que o plugin baixou por último, então liste primeiro:

```sh
ls ~/.claude/plugins/cache/vim-ai-follower/vim-ai-follower/                       # encontre a <versão> instalada
ln -s ~/.claude/plugins/cache/vim-ai-follower/vim-ai-follower/<versão>/bin/claude-follow ~/.local/bin/claude-follow
```

**Se você usou a instalação manual/de desenvolvimento**, o `pip install -e .` já colocou o `claude-follow` no `PATH` do seu shell, então rode direto de um painel dentro da sessão tmux onde o `claude` roda:

```sh
claude-follow start      # abre um painel follower (ou adota um Vim existente)
claude-follow status     # mostra o follower ativo e o arquivo em exibição
claude-follow stop       # desmonta o follower e remove os keybindings
```

`start` aceita:

- `--backend {tmux,nvim}` — padrão `tmux`.
- `--on-failure {silent,reopen}` — o que fazer se o painel do follower morrer no meio da sessão.
- `--speed {instant,muito_rapido,rapido,normal,lento}` — ritmo inicial da animação.
- `--take-keys` — move as teclas de prefixo do tmux para esta instalação mesmo quando outra instalação viva (checkout de dev, pip install, plugin) é a dona; sem ela, o `start` as deixa onde estão e avisa.

Com `open_policy` em `always` ou `code` (veja abaixo), você nem precisa do
`start`: a primeira edição correspondente abre o follower automaticamente.

## Configuração

JSON opcional em `~/.config/claude-vim-follower/config.json`. Toda chave tem um
padrão, e um valor inválido cai silenciosamente para ele.

| Chave           | Valores                                                   | Padrão     | Significado                                                                              |
| --------------- | --------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------- |
| `backend`       | `tmux`, `nvim`                                            | `tmux`     | `tmux`: dirige um Vim inalterado num painel tmux via `send-keys`. `nvim`: dirige um Neovim real via msgpack-RPC (adota um nvim já rodando na janela, ou lança um dedicado — veja Backends). Requer o extra de instalação `nvim`. |
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

Os keybindings são **globais no servidor** tmux, compartilhados por todos os
followers; cada tecla age só no follower da janela onde foi pressionada, e o
último `stop` restaura seus bindings originais.

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
  `send-keys`.
- **`nvim`** (requer `pip install '.[nvim]'`, Neovim ≥ 0.10): dirige um Neovim
  real inteiramente via msgpack-RPC — sem `send-keys`, então a classe de bugs de
  corrupção de teclas que o backend tmux precisa enfrentar simplesmente não
  existe. Toda animação, mudança de velocidade ao vivo, pausa/interrupção e a
  passagem de controle no des-interrupt funcionam igual ao tmux. **Adotar ou
  lançar:** com `adopt_existing: true` (ou `start --backend nvim` numa janela que
  já tem um nvim rodando) ele adota esse nvim — o seu próprio editor, nunca
  travado em somente-leitura; caso contrário lança um nvim headless dedicado para
  a janela. O Neovim também abre abas de verdade — a mesma experiência de
  circular entre abas do tmux, mantida via sua API RPC; a remoção por arquivo
  ainda vale (`max_tabs`).

## Limitações

- Um follower por janela tmux (o estado é indexado pelo id da janela tmux).
- Dois processos `claude` na mesma janela tmux compartilham o follower daquela
  janela.
- Arquivos binários são navegados, não animados.
- Arquivos muito grandes degradam para um paste em bloco quando a animação
  ficaria longa demais, em vez de ritmar tecla a tecla.
