🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/guides/multi-file-tabs/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/guides/multi-file-tabs/)

# Abas multi-arquivo

Cada arquivo que o Claude toca ganha sua própria aba no Vim. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Como o follower lida com edições em vários arquivos

Editar um arquivo já aberto navega de volta pra sua aba primeiro (pelo nome, então sobrevive a você fechar ou reordenar abas), anima ali, e retorna — ele nunca digita na aba que por acaso está ativa.

## Quantas abas ele mantém

A lista de abas é ordenada por uso recente; a aba menos usada recentemente é fechada assim que você passa de `max_tabs` (padrão `5`, veja [Configuração](../reference/config.md)). O arquivo sendo animado ou entregue nunca é o escolhido pra evicção. Num [editor adotado](adopting-an-editor.md) a evicção nunca fecha um buffer com alterações não salvas, nem um que você mesmo tinha aberto.

O Neovim também abre abas de verdade — a mesma experiência de ciclar abas do tmux, mantida em sincronia pela API de RPC; a evicção por arquivo continua valendo (veja [Backend Neovim](nvim-backend.md)).
