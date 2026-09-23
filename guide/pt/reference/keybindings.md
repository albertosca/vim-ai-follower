🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/reference/keybindings/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/reference/keybindings/)

# Atalhos

`start` (e a abertura automática) registram atalhos de **prefixo** do tmux, restaurados no `stop`. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Teclas de prefixo do tmux

| Tecla         | Comando      | Efeito                                                                    |
| ------------- | ------------ | ------------------------------------------------------------------------- |
| `prefix` `P`  | pause        | Pausa uma animação em execução; aperte de novo pra retomar.               |
| `prefix` `S`  | interrupt    | Interrompe: entrega o buffer pra você assumir e salvar sua própria versão. Aperte de novo durante a entrega pra descartar suas edições e retomar a do Claude. |
| `prefix` `+`  | speed-up     | Acelera a animação uma marcação (satura em `instant`).                    |
| `prefix` `_`  | speed-down   | Desacelera a animação uma marcação (satura em `lento`).                   |
| `prefix` `F`  | toggle       | Muta/desmuta o follower.                                                  |

Como cada uma se comporta está no [guia de controles](../guides/controls.md).

## Quem é dono das teclas

Os atalhos são **globais no servidor** tmux, compartilhados por todo follower; cada tecla apertada age só no follower da janela onde foi apertada, e o último `stop` restaura seus atalhos originais.

Quando outra instalação viva (checkout de dev, instalação via pip, plugin) já é dona das teclas, `start` deixa as teclas onde estão e avisa; `start --take-keys` move as teclas pra esta instalação (veja a [referência da CLI](cli.md)).
