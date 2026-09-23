🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/engineering/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/engineering/)

# Engenharia

O vim-ai-follower digita num editor que você está olhando, a partir de hooks que rodam dentro de cada chamada de ferramenta do Claude Code. Então a barra é: nunca corromper o que você vê, nunca atrapalhar o Claude, e nunca digitar na janela errada. Esta página registra as decisões por trás dessa barra, o que cada uma custou, e onde conferir no código. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Como o vim-ai-follower funciona

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://albertosca.github.io/vim-ai-follower/assets/diagrams/how-it-works-dark.svg">
  <img alt="The edit lands on disk first; the follower only replays it; you control it — pause, or take the keyboard." src="https://albertosca.github.io/vim-ai-follower/assets/diagrams/how-it-works-light.svg" width="520">
</picture>

*A edição chega ao disco primeiro; o follower só reproduz. Você continua no controle — pause, ou pegue o teclado.*

`claude-follow` é uma única CLI em Python que é, ao mesmo tempo, a superfície de controle (`start`/`stop`/`status`, pausa/interrupção, velocidade, toggle) e o handler dos hooks do Claude Code. A cada `Edit`/`MultiEdit`/`Write`, um hook `PreToolUse` guarda o conteúdo antigo do arquivo, e um hook `PostToolUse` compara contra o conteúdo novo e reproduz a mudança no follower com `tmux send-keys` (ou via RPC, no [backend nvim](guides/nvim-backend.md)). A cada `Read`, o follower navega até aquele arquivo (e linha).

O buffer do follower fica somente-leitura entre as animações (um Neovim adotado é a exceção: é o seu próprio editor, então nunca é travado), então uma tecla acidental nunca corrompe o que você está vendo; a animação destrava em volta de si mesma e trava de novo (com um resync silencioso do disco) quando termina.

## Decisões e os trade-offs aceitos

| Decisão | Trade-off aceito |
|---|---|
| Dois backends: `send-keys` do tmux pra qualquer Vim, RPC pro Neovim | Duas implementações de um único protocolo de follower, e as colunas do RPC são offsets em bytes |
| Hooks nunca derrubam uma chamada de ferramenta | Uma falha vira uma linha no `hook.log`, não um erro que aparece no Claude |
| A identidade da janela é lembrada, nunca inferida | Quando nada prova qual é a janela, a edição simplesmente não é animada |
| As teclas globais do tmux têm um dono | Uma segunda instalação ativa só ganha as teclas se você passar `--take-keys` |
| 100% de branch coverage como gate, QA visual virado teste e2e | A suíte completa precisa de tmux, Vim e Neovim reais, então o CI roda só a suíte de unidade |

### Dois backends: send-keys do tmux pra qualquer Vim, RPC pro Neovim

O backend `tmux` controla um Vim sem modificações digitando no pane dele com `send-keys`, então funciona com qualquer Vim e qualquer config. O backend `nvim` controla o Neovim inteiramente via msgpack-RPC, então a classe de bug de corrupção por teclado que o backend `tmux` precisa combater simplesmente não existe ali. O custo é uma segunda implementação do mesmo protocolo `Follower`, e o RPC tem suas próprias armadilhas: `nvim_buf_set_text` e `nvim_win_set_cursor` recebem colunas em **bytes**. A animação percorria as linhas por caractere, o que só funciona enquanto a linha é ASCII; um travessão longo transformava `alpha — beta` em `b'alpha \xe2 beta\x80\x94'`, e acentos do português quebravam quase toda linha. Corrigido em [`ffaed50`](https://github.com/albertosca/vim-ai-follower/commit/ffaed50), fixado contra um Neovim headless real por `test_show_fresh_types_multi_byte_content_byte_identically` em [`tests/test_nvim_integration_utf8.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_nvim_integration_utf8.py).

### Hooks nunca derrubam uma chamada de ferramenta

Os hooks são feitos para sair com `0` e registrar problemas em `~/.cache/claude-vim-follower/hook.log`, em vez de falhar a chamada de ferramenta. Uma animação ainda pode morrer no meio do caminho — um timeout de hook mata o processo enquanto você a tinha pausada. O restante e o texto digitado pela metade são persistidos em disco, então o próximo hook reconstrói o buffer a partir desse parcial e retoma, em vez de reinterpretar uma linha digitada pela metade ([`d1620ff`](https://github.com/albertosca/vim-ai-follower/commit/d1620ff); ponta a ponta pela CLI real em `test_crash_fallback_catches_up_after_the_hook_is_killed`, [`tests/test_e2e_cli_controls.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_e2e_cli_controls.py)). O trade-off: quando algo dá errado, a chamada de ferramenta ainda é bem-sucedida e o único rastro é o log.

### A identidade da janela é lembrada, nunca inferida

Um follower pertence a uma janela do tmux, e um hook precisa saber qual. Normalmente `TMUX_PANE` diz isso, mas uma sessão pode perdê-lo no meio da execução. O fallback óbvio, `tmux display-message -p '#{window_id}'`, foi rejeitado: ele responde pela janela que o cliente anexado está olhando, não pela que originou o hook ([`4cda388`](https://github.com/albertosca/vim-ai-follower/commit/4cda388)). Em vez disso, o hook percorre a ancestralidade de processos até um pane, e lembra `session_id` → janela enquanto ainda consegue provar isso, reprovando o servidor tmux antes de confiar numa janela guardada ([`b1a61f0`](https://github.com/albertosca/vim-ai-follower/commit/b1a61f0), [`8c46e02`](https://github.com/albertosca/vim-ai-follower/commit/8c46e02)). `test_a_binding_is_never_reused_across_a_tmux_server_restart` em [`tests/test_integration_session_binding.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_integration_session_binding.py) fixa essa guarda contra um servidor tmux real. O trade-off: quando nada prova qual é a janela, a edição não é animada e o log diz que a sessão perdeu sua identidade de janela do tmux — um chute errado tiparia o arquivo de um projeto no editor de outro.

### As teclas globais do tmux têm um dono

As teclas de prefixo são globais no servidor tmux e embutem um caminho absoluto pro `claude-follow` que as vinculou, então uma atualização do plugin, um checkout de desenvolvimento e uma instalação via pip podem disputar essas teclas. Cada instalação agora se registra como dona das teclas; um hook reaponta as teclas depois de uma atualização do plugin, as teclas de um dono morto são assumidas, e um dono estrangeiro vivo mantém as teclas a menos que você passe `--take-keys` ([`59e22d1`](https://github.com/albertosca/vim-ai-follower/commit/59e22d1), [`a9368cb`](https://github.com/albertosca/vim-ai-follower/commit/a9368cb)). A revisão final de branch inteira encontrou depois dois buracos de posse — entre eles, o comando `/start` e os hooks registravam o mesmo plugin como duas instalações diferentes, então as teclas nunca eram reapontadas depois de uma atualização ([`de74e9e`](https://github.com/albertosca/vim-ai-follower/commit/de74e9e); `test_hook_post_repoints_keys_after_a_plugin_update` em [`tests/test_keybindings_ownership.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_keybindings_ownership.py)).

### 100% de branch coverage como gate, QA visual virado teste e2e

A suíte completa falha abaixo de 100% de branch coverage. O custo é um teste pra cada branch novo, e uma suíte que precisa de tmux, Vim e Neovim reais: ela leva uns sete minutos e roda localmente, enquanto o CI roda a suíte de unidade. Coverage não enxerga o que um usuário de fato executa — o wrapper `bin/claude-follow` como subprocesso, o payload do hook como JSON no stdin — então a bateria manual de QA visual foi convertida em testes e2e pela CLI real, cada um num mundo isolado com seu próprio `HOME` e servidor tmux ([`431526f`](https://github.com/albertosca/vim-ai-follower/commit/431526f); todo teste dessa primeira leva foi canariado revertendo o fix que ele guarda). A conversão encontrou um bug que a bateria manual nunca mostrou, porque sua checagem disparava uma única edição: no Neovim, o aviso "Writing..." só aparecia na primeira animação. Um `:bwipeout!` malformado causava isso, e o teste de unidade tinha escondido o problema modelando o buffer como um inteiro puro ([`4e73cb1`](https://github.com/albertosca/vim-ai-follower/commit/4e73cb1); `test_first_animation_shows_writing_and_keeps_the_typing_highlight` em [`tests/test_e2e_battery_tranche2.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_e2e_battery_tranche2.py) agora dispara uma segunda animação).

## Onde o código vive

| Módulo | Propósito |
|---|---|
| `cli.py`, `commands.py`, `keybindings.py` | Subcomandos, sua implementação, as teclas de prefixo do tmux |
| `hooks.py` | Orquestração dos hooks: animar, entrega por interrupção, retomada após queda |
| `control.py` | Sinais e animações pendentes persistidas (com o seu parcial) |
| `binding.py` | O armazenamento `session_id` → janela |
| `state.py` | Estado do follower por janela |
| `snapshot.py`, `diff.py` | Snapshots de antes/depois e o script de edição |
| `backends/tmux_vim.py`, `backends/nvim.py` | As duas implementações do protocolo `Follower` |

Tudo em [`src/vim_ai_follower/`](https://github.com/albertosca/vim-ai-follower/tree/main/src/vim_ai_follower). Objeções e limites estão no [FAQ](faq.md).
