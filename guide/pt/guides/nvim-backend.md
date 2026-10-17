🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/guides/nvim-backend/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/guides/nvim-backend/)

# Backend Neovim

O vim-ai-follower tem dois backends. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

- **`tmux`** (padrão): controla um Vim sem modificações num pane de tmux via `send-keys`.
- **`nvim`** (precisa do pynvim, Neovim ≥ 0.10): controla um Neovim de verdade inteiramente via msgpack-RPC.

## Como usar o Claude Code com Neovim

1. Instale o pynvim no `python3` do seu `PATH`: `pip install pynvim` (instalação via plugin), ou `pip install -e '.[nvim]'` a partir de um clone (veja [Instalação](../getting-started/install.md)).
2. Inicie o follower com o backend nvim: `/vim-ai-follower:start --backend nvim`, ou defina `"backend": "nvim"` no [arquivo de configuração](../reference/config.md).

## O que o backend nvim faz diferente

O backend `nvim` controla um Neovim de verdade inteiramente via msgpack-RPC — sem `send-keys`, então a classe de bug de corrupção por teclado que o backend tmux precisa combater simplesmente não existe. Toda animação, mudança de velocidade ao vivo, pausa/interrupção e a entrega de volta funcionam do mesmo jeito que no tmux.

**Adotar ou lançar:** com `adopt_existing: true` (ou `start --backend nvim` numa janela que já tem um nvim rodando), ele adota esse nvim — o seu próprio editor, nunca travado como somente-leitura; senão, lança um nvim headless dedicado pra aquela janela.

**Quando o nvim lançado não sobe:** um nvim dedicado precisa responder por RPC e terminar de iniciar em até 3 s (10 s no `start`). Um que não faz isso (uma config que para num erro de "Press ENTER", uma que trava, um crash) não é registrado como follower: o pane dele fica aberto pra você ler o erro do próprio nvim, a mensagem diz isso (o `start` sai com código diferente de zero e nunca diz "attached"), e a abertura automática não divide outro nvim pra aquela janela por 5 minutos. O `start` tenta de novo na hora, mas nunca divide um segundo nvim ao lado de um que ainda ocupa o socket: ele avisa que esse nvim não terminou de iniciar (ou não responde), então aperte Enter no erro dele ou feche o pane. Depois que você apertar Enter ali, a próxima edição se conecta a esse mesmo pane.

O Neovim também abre abas de verdade — a mesma experiência de ciclar abas do tmux, mantida em sincronia pela API de RPC; a evicção por arquivo continua valendo (`max_tabs`, veja [Abas multi-arquivo](multi-file-tabs.md)).

Por que dois backends, e o que a rota RPC custou, está na página de [Engenharia](../engineering.md).
