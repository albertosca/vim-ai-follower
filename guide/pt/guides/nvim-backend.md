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

O Neovim também abre abas de verdade — a mesma experiência de ciclar abas do tmux, mantida em sincronia pela API de RPC; a evicção por arquivo continua valendo (`max_tabs`, veja [Abas multi-arquivo](multi-file-tabs.md)).

Por que dois backends, e o que a rota RPC custou, está na página de [Engenharia](../engineering.md).
