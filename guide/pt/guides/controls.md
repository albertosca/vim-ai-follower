🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/guides/controls/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/guides/controls/)

# Pausar, assumir, velocidade e mudo

Cinco teclas de prefixo do tmux controlam um follower em execução: pausar (`P`), interromper pra pegar o teclado (`S`), mais rápido (`+`), mais lento (`_`) e mudo (`F`). Cada tecla age só no follower da janela onde foi apertada. A tabela completa de teclas está em [Atalhos](../reference/keybindings.md). Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Como pausar as edições do Claude Code no Vim

**Pausar** (`P`) segura a vez do Claude no lugar até você retomar — a animação termina visualmente antes do Claude continuar.

1. Enquanto uma edição está animando, aperte `prefix` `P`.
2. Leia, role pra trás, pense. A vez do Claude espera.
3. Aperte `prefix` `P` de novo pra retomar.

Se o processo do hook for morto (por exemplo, um timeout de hook) enquanto pausado, uma retomada após queda deixa o teclado terminar a animação.

## Como assumir uma edição no meio da animação

**Interromper** (`S`) entrega o buffer pra você: edite e salve a sua versão, e o Claude é avisado de que você assumiu (ele relê a sua versão do disco em vez de restaurar a dele).

1. Enquanto uma edição está animando, aperte `prefix` `S`. O follower entrega o buffer pra você.
2. Edite, depois salve a sua versão pra devolver o controle. O Claude é avisado de que você assumiu, e relê a sua versão do disco.
3. Mudou de ideia? Apertar `S` de novo durante a entrega descarta suas edições não salvas e retoma o acompanhamento do arquivo que o Claude escreveu.

## Como acelerar ou desacelerar a animação

`prefix` `+` / `prefix` `_` releem o ritmo na hora: uma animação em execução acelera ou desacelera na **próxima quebra de linha**, não só na próxima animação. A escala satura nas duas pontas; o popup identifica os limites com o texto literal da CLI — `lento (slowest)` (o mais lento) e `instant (fastest)` (o mais rápido).

1. Aperte `prefix` `+` pra acelerar uma marcação, ou `prefix` `_` pra desacelerar uma marcação.
2. Pra escolher o ritmo com que um follower começa, passe `--speed` pro [`start`](../reference/cli.md) ou defina `speed` no [arquivo de configuração](../reference/config.md).

## Como mutar o follower

`prefix` `F` muta o follower: as próximas edições são ignoradas e o pane de origem é ampliado (zoom) pra o follower sair do caminho. Apertar `F` de novo desmuta, tira o zoom, e força a próxima edição a **resincronizar via retipagem completa** — o arquivo mudou no disco enquanto estava mutado, então animar um diff contra o buffer velho produziria lixo. As abas já abertas continuam abertas pra leitura.

1. Aperte `prefix` `F` (ou rode `/vim-ai-follower:toggle`) pra mutar.
2. Aperte de novo pra desmutar; a próxima edição retipa o arquivo inteiro.
