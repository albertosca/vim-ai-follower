🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/reference/cli/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/reference/cli/)

# Linha de comando

`claude-follow` é uma única CLI em Python que é, ao mesmo tempo, a superfície de controle (`start`/`stop`/`status`, pausa/interrupção, velocidade, toggle) e o handler dos hooks do Claude Code. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Comandos de barra (instalação via plugin)

**Se você instalou o plugin**, controle ele pelos comandos de barra, a partir de um pane dentro da sessão tmux onde o `claude` está rodando: `/vim-ai-follower:start`, `/vim-ai-follower:status`, `/vim-ai-follower:stop`, `/vim-ai-follower:toggle`. `/vim-ai-follower:start` repassa seus argumentos direto pro `claude-follow start` (flags abaixo), por exemplo `/vim-ai-follower:start --backend nvim --speed lento`.

## Subcomandos

| Comando | O que faz |
|---|---|
| `claude-follow start` | Abre um pane de follower (ou adota um Vim já existente). |
| `claude-follow status` | Mostra o follower ativo e o arquivo que ele está mostrando. |
| `claude-follow stop` | Desmonta o follower e remove os atalhos. |
| `claude-follow pause` | Pausa ou retoma uma animação em execução (ligado a `prefix` `P`). |
| `claude-follow interrupt` | Entrega o buffer pra você, ou descarta suas edições de entrega (ligado a `prefix` `S`). |
| `claude-follow speed-up` / `speed-down` | Muda o ritmo da animação (ligado a `prefix` `+` / `prefix` `_`). |
| `claude-follow toggle` | Muta/desmuta o follower (ligado a `prefix` `F`). |
| `claude-follow hook pre` / `hook post` | O handler de hook que o Claude Code chama; lê o payload do hook como JSON no stdin. |

As teclas estão descritas em [Atalhos](keybindings.md).

## Flags do `start`

`start` aceita:

- `--backend {tmux,nvim}` — padrão `tmux`.
- `--on-failure {silent,reopen}` — o que fazer se o pane do follower morrer no meio da sessão.
- `--speed {instant,muito_rapido,rapido,normal,lento}` — ritmo inicial da animação.
- `--take-keys` — move as teclas de prefixo do tmux pra esta instalação mesmo quando outra instalação viva (checkout de dev, instalação via pip, plugin) já é dona delas; sem essa flag, `start` deixa as teclas onde estão e avisa.

Sem nenhuma flag, `start` recorre à chave correspondente no [arquivo de configuração](config.md).

## Como rodar o claude-follow direto do seu shell

O wrapper `claude-follow` que vem empacotado é, de propósito, não colocado no `PATH` do seu shell — ele só roda a partir da própria ferramenta Bash do Claude Code e dos atalhos do tmux (veja o comentário de cabeçalho do `bin/claude-follow`). Se você também quiser rodar o `claude-follow` direto do seu shell, crie um symlink do wrapper empacotado pra qualquer diretório no seu `PATH` (por exemplo `~/.local/bin`) — o diretório de versão varia conforme o que o plugin baixou por último, então liste primeiro:

```sh
ls ~/.claude/plugins/cache/vim-ai-follower/vim-ai-follower/                       # encontre a <versão> instalada
ln -s ~/.claude/plugins/cache/vim-ai-follower/vim-ai-follower/<versão>/bin/claude-follow ~/.local/bin/claude-follow
```

**Se você usou a instalação manual/de desenvolvimento**, `pip install -e .` já colocou o `claude-follow` no `PATH` do seu shell.
