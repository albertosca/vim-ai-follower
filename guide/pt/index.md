🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/)

# vim-ai-follower

**Veja o Claude Code digitar no seu próprio Vim.**

Cada edição é reproduzida linha a linha, num ritmo que dá pra acompanhar de verdade — pause, pegue o teclado, devolva.

<!-- facts -->900+ testes de unidade no CI · 100% de branch coverage na suíte completa · backends Vim + Neovim · v0.2.9<!-- /facts -->

**[Comece agora →](getting-started/install.md)**

## Veja funcionando

<video src="https://albertosca.github.io/vim-ai-follower/assets/demo/follow.mp4" autoplay loop muted playsinline controls width="100%" aria-label="O Claude escreve fib.py no Vim do follower linha a linha; a animação é pausada e retomada; um Edit adiciona uma função, animado como diff; no arquivo seguinte o teclado é tomado, uma linha é adicionada e salva, e isso devolve a vez ao Claude."></video>

*Payloads de hook roteirizados, follower real — gravado com vhs a partir de [`assets/demo/follow.tape`](https://github.com/albertosca/vim-ai-follower/blob/main/assets/demo/follow.tape).*

## Como funciona

<img alt="A edição chega ao disco primeiro; o follower só a reproduz; você controla — pause, ou pegue o teclado." src="https://albertosca.github.io/vim-ai-follower/assets/diagrams/how-it-works-light.svg#gh-light-mode-only" width="520">
<img alt="A edição chega ao disco primeiro; o follower só a reproduz; você controla — pause, ou pegue o teclado." src="https://albertosca.github.io/vim-ai-follower/assets/diagrams/how-it-works-dark.svg#gh-dark-mode-only" width="520">

- O `Edit` ou o `Write` do Claude Code chega ao disco primeiro; um hook `PreToolUse` já tinha guardado o conteúdo antigo.
- Um hook `PostToolUse` compara o conteúdo antigo com o novo e reproduz a mudança no seu Vim (`tmux send-keys`) ou no Neovim (RPC).
- Num `Read`, o follower navega até aquele arquivo e linha.
- Você continua no controle: `prefix` `P` pausa, `prefix` `S` pega o teclado, e salvar a sua versão devolve o controle.

## O que ele não faz

- **Nunca escreve nos seus arquivos.** Os buffers do follower nunca são salvos, e ficam travados como somente-leitura entre as animações (um Neovim adotado é a exceção: é o seu próprio editor, então nunca é travado).
- **Nunca derruba uma chamada de ferramenta do Claude Code.** Os hooks são feitos para sair com `0` e registrar problemas em `hook.log`, em vez de falhar a chamada. A animação leva tempo mesmo — é pra isso que servem as teclas de velocidade e o mudo.
- **Nada sai da sua máquina.** O pacote não importa nenhum módulo de rede, e o Neovim é acessado por um socket local.

## Construído com cuidado

Dois backends, hooks que nunca derrubam uma chamada de ferramenta, uma identidade de janela que é lembrada em vez de adivinhada, e um gate de 100% de branch coverage apoiado em testes ponta a ponta pela CLI real. As decisões, e o que cada uma custou, estão na página de [Engenharia](engineering.md).

## Instalação

```
/plugin marketplace add albertosca/vim-ai-follower
/plugin install vim-ai-follower
```

Depois rode `/vim-ai-follower:start` dentro da sessão tmux onde o `claude` está rodando. Requisitos, o extra do Neovim e a instalação manual estão no [guia de instalação](getting-started/install.md).

## Para onde ir agora

- [Instalação](getting-started/install.md) — requisitos, o plugin, a instalação manual
- [Acompanhe sua primeira edição](getting-started/first-follow.md) — inicie o follower e veja uma edição chegar
- [Pausar, assumir, velocidade e mudo](guides/controls.md) — as cinco teclas de tmux que controlam uma animação em andamento
- [Abas multi-arquivo](guides/multi-file-tabs.md) — uma aba de Vim por arquivo, limitada por `max_tabs`
- [Adotando um editor](guides/adopting-an-editor.md) — deixe o Claude digitar no Vim que você já tem aberto
- [Backend Neovim](guides/nvim-backend.md) — controle o Neovim via RPC em vez de teclas simuladas
- [Linha de comando](reference/cli.md), [Configuração](reference/config.md), [Atalhos](reference/keybindings.md) — a referência
- [FAQ](faq.md) — se deixa o Claude mais lento, se toca nos seus arquivos, limitações atuais

---

Feito por Alberto Cavalcanti — [Conecte-se no LinkedIn](https://www.linkedin.com/in/albertosca/) · [Código no GitHub](https://github.com/albertosca/vim-ai-follower) · licença MIT
