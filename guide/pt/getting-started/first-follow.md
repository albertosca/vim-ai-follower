🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/getting-started/first-follow/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/getting-started/first-follow/)

# Acompanhe sua primeira edição

Um pane dedicado do tmux (ou um Neovim em execução) espelha cada arquivo que o Claude Code lê ou escreve, animando cada mudança linha a linha, como se o Claude estivesse digitando no seu próprio editor. Esta página leva você de um plugin instalado até a primeira edição animada. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Como acompanhar o Claude Code editando um arquivo no Vim

1. [Instale o plugin](install.md), e rode o `claude` dentro de uma sessão tmux.
2. No Claude Code, na sessão tmux onde o `claude` está rodando, inicie o follower: `/vim-ai-follower:start`. Ele abre um pane de follower (ou adota um Vim já existente, com [`adopt_existing`](../guides/adopting-an-editor.md)).
3. Peça ao Claude pra editar um arquivo. A cada `Edit`/`MultiEdit`/`Write`, um hook `PreToolUse` guarda o conteúdo antigo do arquivo e um hook `PostToolUse` compara contra o conteúdo novo e reproduz a mudança no follower. A cada `Read`, o follower navega até aquele arquivo (e linha).
4. Confira o que está sendo mostrado com `/vim-ai-follower:status`, e desmonte tudo com `/vim-ai-follower:stop`.

`/vim-ai-follower:start` repassa seus argumentos direto pro `claude-follow start` (as flags estão na [referência da CLI](../reference/cli.md)), por exemplo `/vim-ai-follower:start --backend nvim --speed lento`. `/vim-ai-follower:toggle` muta e desmuta o follower.

O buffer do follower fica somente-leitura entre as animações (um Neovim adotado é a exceção: é o seu próprio editor, então nunca é travado), então uma tecla acidental nunca corrompe o que você está vendo; a animação destrava em volta de si mesma e trava de novo (com um resync silencioso do disco) quando termina.

## Como abrir o follower automaticamente

Com `open_policy` definido como `always` ou `code` (veja [Configuração](../reference/config.md)), você nem precisa do `start`: a primeira edição compatível já abre o follower sozinho.

## Se você usou a instalação manual / de desenvolvimento

`pip install -e .` já colocou o `claude-follow` no `PATH` do seu shell, então rode ele direto de um pane dentro da sessão tmux onde o `claude` está rodando:

```sh
claude-follow start      # abre um pane de follower (ou adota um Vim já existente)
claude-follow status     # mostra o follower ativo e o arquivo que ele está mostrando
claude-follow stop       # desmonta o follower e remove os atalhos
```

Próximo passo: [pausar, assumir e mudar a velocidade](../guides/controls.md).
