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

## Quando o seu buffer não é o que o Claude editou

O follower anima a edição do Claude como um diff sobre o buffer que tem o arquivo. No seu próprio editor esse buffer pode ter outra coisa: linhas que você digitou e não salvou, ou uma cópia antiga de um arquivo que um formatador ou um `git checkout` reescreveu. Redigitar o arquivo apagaria esse buffer primeiro, e um diff cairia sobre o texto errado, então um editor adotado não recebe nenhum dos dois:

- O buffer fica exatamente como está, com a digitação não salva, e a edição do Claude não é animada.
- O aviso de status (a borda do pane no tmux, a caixa flutuante no Neovim) mostra `buffer differs — :e! shows Claude's edit`, e o evento vai pro `~/.cache/claude-vim-follower/hook.log`. O aviso fica até o follower animar a próxima edição naquela janela.
- A edição do Claude já está no disco. Rode `:e!` nesse buffer pra carregá-la (isso descarta o que você digitou e não salvou ali), ou salve a sua versão antes.
- Quando o buffer volta a bater com o arquivo, a próxima edição dele é animada normalmente.

Isso vale também pra um arquivo que você mesmo abriu antes de o Claude editá-lo pela primeira vez. Um follower dedicado (não adotado) é dono dos buffers dele e redigita o arquivo inteiro.

## Riscos enquanto anima

- **Disciplina:** pause (`P`) antes de navegar por aí durante uma animação. Uma animação adotada controla seu cursor ao vivo, e digitar ou trocar de aba no meio de uma animação pode se intercalar com as teclas injetadas.
- Risco residual: o follower não consegue distinguir suas teclas das dele no nível do tty, então uma edição mal cronometrada durante uma animação não pausada ainda pode acabar no lugar errado. O travamento somente-leitura protege o buffer entre as animações, não durante uma que você interrompe digitando. Esse travamento vale pra um Vim adotado no backend tmux; um Neovim adotado nunca é travado como somente-leitura, porque é o seu próprio editor.
