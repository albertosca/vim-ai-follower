🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/reference/cli/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/reference/cli/)

# Linha de comando

`claude-follow` é uma única CLI em Python que é, ao mesmo tempo, a superfície de controle (`start`/`stop`/`status`, pausa/interrupção, velocidade, toggle) e o handler dos hooks do Claude Code. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Comandos de barra (instalação via plugin)

**Se você instalou o plugin**, controle ele pelos comandos de barra, a partir de um pane dentro da sessão tmux onde o `claude` está rodando: `/vim-ai-follower:start`, `/vim-ai-follower:status`, `/vim-ai-follower:stop`, `/vim-ai-follower:toggle`. `/vim-ai-follower:start` repassa seus argumentos direto pro `claude-follow start` (flags abaixo), por exemplo `/vim-ai-follower:start --backend nvim --speed lento`.

## Subcomandos

| Comando | O que faz |
|---|---|
| `claude-follow start` | Abre um pane de follower (ou adota um Vim já existente). Um nvim lançado que não responde em 10 s é reportado, com o pane dele aberto mostrando o erro do próprio nvim, e nada é registrado (código de saída 1). |
| `claude-follow status` | Mostra o follower ativo e o arquivo que ele está mostrando. |
| `claude-follow stop` | Desmonta o follower e remove os atalhos. Vale para esta janela até o próximo `start` (ou toggle) — a abertura automática não a reabre. Tem limite de tempo: um nvim que não responde (ocupado ou congelado) não recebe nada, nem um nvim seu (adotado) no meio de uma sequência de teclas ou na janela de linha de comando (uma linha no `hook.log` avisa); num nvim seu em que você está digitando, selecionando, confirmando, na linha de comando ou com um operador pendente, o `stop` fecha só as abas do follower em que você não está e te deixa exatamente onde estava (uma linha no `hook.log` cita uma aba do follower que ficou porque você está nela); o nvim do próprio follower parado num prompt tem o prompt dispensado e é fechado, um nvim que para de responder no meio da desmontagem segura o `stop` por no máximo uns 13 s, e um nvim do follower que não fecha tem o próprio pane fechado. Um pane só é fechado ou recebe teclas depois de provado que é do follower: um estado deixado por um servidor tmux anterior (os ids de pane recomeçam depois de um restart), ou que aponta para um pane que não roda mais o Vim, é limpo com uma linha no `hook.log` e nada é enviado. Um hook esperando o seu save depois de uma interrupção (o hand-off) termina em menos de um segundo depois do `stop` e avisa o Claude que a versão dele vale. |
| `claude-follow pause` | Pausa ou retoma uma animação em execução (ligado a `prefix` `P`). |
| `claude-follow interrupt` | Entrega o buffer pra você, ou descarta suas edições de entrega (ligado a `prefix` `S`). |
| `claude-follow speed-up` / `speed-down` | Muda o ritmo da animação (ligado a `prefix` `+` / `prefix` `_`). |
| `claude-follow toggle` | Muta/desmuta o follower (ligado a `prefix` `F`). Depois de um `stop`, religa a abertura automática da janela sem abrir nada. |
| `claude-follow hook pre` / `hook post` / `hook failure` | O handler de hook que o Claude Code chama; lê o payload do hook como JSON no stdin. O `hook failure` roda quando uma edição falha ou é negada, pra que um Read desse arquivo não fique mais retido. |

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
