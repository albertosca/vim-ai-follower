🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/faq/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/faq/)

# FAQ

Respostas diretas para as perguntas que as pessoas fazem antes de deixar uma ferramenta digitar no editor delas, e depois as limitações atuais. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Perguntas

### O que é o vim-ai-follower?

Um plugin do Claude Code que reproduz cada edição de arquivo que o Claude faz num Vim ou Neovim de verdade, linha a linha, num ritmo que dá pra acompanhar. Os hooks `PreToolUse` e `PostToolUse` guardam o arquivo, comparam, e animam a mudança num pane de tmux rodando Vim, ou num Neovim via RPC. Você pode pausar, pegar o teclado no meio da edição, e devolver. Veja [como funciona](engineering.md#como-o-vim-ai-follower-funciona).

### Isso deixa o Claude mais lento?

Sim, enquanto anima. A edição do Claude já está no disco quando a animação começa, mas o hook segura a vez do Claude até a animação terminar, então uma edição longa num ritmo lento consome tempo real. Pra velocidade, `prefix` `+` acelera o ritmo, uma marcação de cada vez, até `instant` (sem ritmo algum), e `prefix` `F` muta o follower: enquanto mutado, as edições não são animadas. `prefix` `S` não é um controle de velocidade: ele para a animação e entrega o buffer pra você, e a vez do Claude fica esperando até você salvar a sua versão (use `:w!` se o Vim acusar E13 ao devolver) ou apertar `S` de novo pra descartar suas edições e deixar a animação retomar. Veja [Pausar, assumir, velocidade e mudo](guides/controls.md).

### Isso toca nos meus arquivos?

Não. A edição chega ao seu arquivo pelo próprio `Edit`/`Write` do Claude Code; o follower só reproduz. Os buffers do follower nunca são salvos, e entre as animações ficam travados como somente-leitura, então uma tecla acidental não consegue mudar o que você está vendo (um Neovim adotado é a exceção: é o seu próprio editor, então nunca é travado). A única vez que um arquivo é escrito a partir do follower é quando você assume com `prefix` `S` e salva a sua própria versão.

### Isso pode derrubar uma chamada de ferramenta do Claude Code?

Não. Os hooks são feitos para sair com `0` e registrar problemas em `~/.cache/claude-vim-follower/hook.log`, em vez de falhar a chamada de ferramenta. O trade-off é que uma falha fica silenciosa pro Claude: se o follower não fizer nada, leia o log.

### Alguma coisa sai da minha máquina?

Não. O pacote não importa nenhum módulo de rede; o tmux é controlado pela própria CLI dele, e o Neovim é acessado por um socket local. Não existe servidor nem telemetria.

### Funciona com Neovim?

Sim. O backend `nvim` controla o Neovim ≥ 0.10 via msgpack-RPC, e pode adotar o nvim que você já tem aberto. Veja [Backend Neovim](guides/nvim-backend.md).

### Dá pra digitar no Vim que eu já tenho aberto?

Sim, é opt-in, com `adopt_existing: true`. Pause antes de navegar durante uma animação: uma animação adotada controla seu cursor ao vivo. Veja [Adotando um editor](guides/adopting-an-editor.md).

### Preciso de tmux?

Pro backend padrão `tmux`, sim: o `claude` precisa rodar dentro de uma sessão tmux, e os controles são teclas de prefixo do tmux. Veja [Instalação](getting-started/install.md).

## Limitações

- Um follower por janela de tmux (o estado é indexado pelo id da janela do tmux).
- Dois processos `claude` na mesma janela de tmux compartilham o follower daquela janela.
- Arquivos binários recebem navegação, não animação.
- No backend tmux, uma única linha ou bloco alterado cujas próprias teclas levariam mais de 60 segundos no ritmo escolhido é enviado sem ritmo a partir daquele ponto.
