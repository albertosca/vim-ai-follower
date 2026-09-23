🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/guides/adopting-an-editor/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/guides/adopting-an-editor/)

# Adotando um Vim já existente (opt-in)

Com `adopt_existing: true`, em vez de abrir um pane novo, o follower controla um Vim que você já tem aberto na mesma janela do tmux. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Como fazer o Claude Code digitar no Vim que você já tem aberto

1. Defina `"adopt_existing": true` em `~/.config/claude-vim-follower/config.json` (veja [Configuração](../reference/config.md)).
2. Abra o Vim (ou o Neovim, pro [backend nvim](nvim-backend.md)) na janela do tmux onde o `claude` está rodando.
3. Rode `/vim-ai-follower:start`, ou deixe o `open_policy` abrir automaticamente na primeira edição.

## O que a adoção garante

Porque é **o seu** editor:

- A adoção é estritamente opt-in.
- A aba em que você estava nunca é renomeada por cima — um arquivo novo sempre abre na sua própria aba.
- No `stop`, a adoção nunca mata o seu Vim; ela só fecha as abas que abriu.

## Riscos enquanto anima

- **Disciplina:** pause (`P`) antes de navegar por aí durante uma animação. Uma animação adotada controla seu cursor ao vivo, e digitar ou trocar de aba no meio de uma animação pode se intercalar com as teclas injetadas.
- Risco residual: o follower não consegue distinguir suas teclas das dele no nível do tty, então uma edição mal cronometrada durante uma animação não pausada ainda pode acabar no lugar errado. O travamento somente-leitura protege o buffer entre as animações, não durante uma que você interrompe digitando. Esse travamento vale pra um Vim adotado no backend tmux; um Neovim adotado nunca é travado como somente-leitura, porque é o seu próprio editor.
