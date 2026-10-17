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
- A aba em que você estava nunca é renomeada por cima — um arquivo novo sempre abre na sua própria aba, no Vim e no Neovim, inclusive o primeiro arquivo que o Claude escreve. No Vim o follower só dá nome ao buffer vazio com que essa aba foi criada; no Neovim ele cria um buffer só dele. Se um autocommand seu te leva pra outra aba quando ela abre, o seu buffer continua intacto: no Vim essa edição não é mostrada (fica registrada em `~/.cache/claude-vim-follower/hook.log`), no Neovim ela vai mesmo assim pro buffer do próprio follower, na aba nova do follower, e o mesmo vale pra um arquivo que o Claude lê: cada janela sua continua com o seu buffer.
- No `stop`, a adoção nunca mata o seu Vim; ela só fecha as abas que abriu.
- Nem o `stop` nem uma aba que passa de `max_tabs` descartam o seu trabalho. Um buffer com alterações não salvas continua aberto onde está, e um buffer que você já tinha aberto antes de o follower chegar nele (por exemplo, um arquivo que você estava editando quando o Claude o leu) nunca é fechado: no máximo o follower fecha uma aba que ele abriu pra mostrá-lo. Cada caso desses fica registrado no `hook.log`. Só um arquivo que o próprio follower abriu, sem nada não salvo, tem a aba fechada.
- Um prompt "Press ENTER" ou "-- More --" no seu editor só é respondido enquanto o follower está agindo ali (abrindo um arquivo, digitando uma edição, o `stop` fechando as abas dele), no Vim e no Neovim, do jeito que uma tecla sua o responderia. Enquanto o follower está parado, pausado ou esperando você salvar, os seus prompts continuam abertos. Um prompt que pede uma escolha sua (`confirm()`, a pergunta do arquivo de swap, `:s///c`) ou que você digite (`input()`) nunca é respondido no Neovim: ele continua aberto, e a edição é pulada, porque a sua resposta cairia no buffer que o follower tivesse acabado de abrir. O mesmo vale para um comando `:` ou uma busca que você está no meio de digitar, e para uma sequência de teclas no meio (um `q` esperando o registrador, por exemplo): a edição é pulada na hora, registrada no `hook.log`, e o follower alcança o arquivo na próxima vez que o Claude mexer nele.
- Se você está digitando no seu Vim ou Neovim quando o Claude edita ou lê um arquivo (modo insert, replace, terminal, visual ou select), o follower pergunta antes, num popup do tmux: aperte `y` pra deixá-lo assumir o editor (ele sai do seu modo e mostra a edição). Qualquer outra tecla, ou nenhuma resposta dentro de `take_over_question_seconds` (10 s por padrão), deixa o seu editor exatamente como estava e pula aquela edição; uma tecla que você já estava digitando quando o popup apareceu conta como não. Sem nenhum cliente do tmux conectado pra mostrar o popup, a edição é pulada sem perguntar. Um Vim que o follower nunca alcançou só é reconhecido como digitando pelo que a tela dele mostra: o `-- INSERT --` que o 'showmode' (ligado por padrão) põe na última linha, ou um cursor em barra ou sublinhado; da primeira edição ou leitura em diante, o script do follower informa o seu modo diretamente. O `claude-follow stop` nunca pergunta: deixa intocado um editor em que você está digitando.

## Quando o seu buffer não é o que o Claude editou

O follower anima a edição do Claude como um diff sobre o buffer que tem o arquivo. No seu próprio editor esse buffer pode ter outra coisa: linhas que você digitou e não salvou, ou uma cópia antiga de um arquivo que um formatador ou um `git checkout` reescreveu. Redigitar o arquivo apagaria esse buffer primeiro, e um diff cairia sobre o texto errado, então um editor adotado não recebe nenhum dos dois:

- O buffer fica exatamente como está, com a digitação não salva, e a edição do Claude não é animada. O mesmo vale pra uma animação que foi interrompida ou pausada e nunca terminou: o que sobrou dela é descartado, em vez de ser reaplicado por cima do que você digitou depois. Se você não digitou nada, a sobra é terminada primeiro; e se o arquivo também foi alterado fora do Claude nesse meio-tempo, esse buffer, que então só tem o texto do próprio follower, é recarregado do disco e mostra o arquivo final do Claude sem animá-lo. O follower confere de novo logo antes dessa recarga: se você digitou no buffer enquanto a sobra era terminada, ele fica como está, como qualquer outro, com o aviso.
- O aviso de status (a borda do pane no tmux, a caixa flutuante no Neovim) mostra `buffer differs — :e! shows Claude's edit`, e o evento vai pro `~/.cache/claude-vim-follower/hook.log`. O aviso fica até o follower começar a animar a próxima edição naquela janela, de qualquer arquivo, ou recarregar um buffer do disco depois de terminar uma sobra, como descrito acima; qualquer um dos dois tira o aviso.
- A edição do Claude já está no disco. `:e!` nesse buffer carrega ela e descarta o que você digitou e não salvou ali.
- Salvar (`:w`) faz o contrário: grava o seu buffer, o texto antigo mais a sua digitação, por cima do arquivo que o Claude acabou de escrever, e a edição do Claude se perde. Pra ficar com as duas, copie a sua digitação pra outro lugar (outro buffer, um registrador), rode `:e!` e aplique a sua digitação de novo.
- Quando o buffer volta a bater com o arquivo, a próxima edição dele é animada normalmente.

Se o follower não consegue conferir o buffer (o seu editor não respondeu à consulta dele, por exemplo um Vim no backend tmux que não consegue escrever em `~/.cache/claude-vim-follower/`), ele joga no seguro do mesmo jeito: a edição não é mostrada e o aviso mostra `can't check buffer — edit not shown`. `:e!` não resolve esse. Quando a consulta fica sem resposta, ele se repete a cada edição enquanto isso durar, e o `hook.log` registra o motivo. Um Vim que deixa a navegação do follower sem resposta nenhuma (travado num `:!make`, por exemplo) não é consultado de novo por 30 segundos: as edições nesse intervalo são puladas na hora, com o tempo restante no `hook.log`, em vez de cada uma esperar 10 segundos por ele. O mesmo aviso também aparece uma única vez, sem se repetir, quando uma sobra de uma animação interrompida ou pausada cujo hook foi encerrado não registrou nada para conferir o seu buffer: a sobra é descartada em vez de reproduzida, o seu buffer fica como está, e `:e!` mostra a edição do Claude.

Isso vale também pra um arquivo que você mesmo abriu antes de o Claude editá-lo pela primeira vez. Um follower dedicado (não adotado) é dono dos buffers dele e redigita o arquivo inteiro.

## Riscos enquanto anima

- **Disciplina:** pause (`P`) antes de navegar por aí durante uma animação. Uma animação adotada controla seu cursor ao vivo, e digitar ou trocar de aba no meio de uma animação pode se intercalar com as teclas injetadas.
- Risco residual: o follower não consegue distinguir suas teclas das dele no nível do tty, então uma edição mal cronometrada durante uma animação não pausada ainda pode acabar no lugar errado. O travamento somente-leitura protege o buffer entre as animações, não durante uma que você interrompe digitando. Esse travamento vale pra um Vim adotado no backend tmux; um Neovim adotado nunca é travado como somente-leitura, porque é o seu próprio editor.
