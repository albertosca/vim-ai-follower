🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/getting-started/install/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/getting-started/install/)

# Instalar o vim-ai-follower

O vim-ai-follower se instala como um plugin do Claude Code que conecta os próprios hooks, e funciona em qualquer projeto onde o `claude` rode dentro do tmux. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Requisitos

- tmux, com o `claude` rodando dentro de uma sessão tmux
- Vim (o backend padrão `tmux`) ou Neovim ≥ 0.10 (o backend `nvim` de primeira classe)
- Python 3.11+

## Como instalar o plugin do Claude Code

O plugin declara os próprios hooks, então não existe `settings.json` pra editar. Essa é a instalação recomendada.

1. No Claude Code, adicione o marketplace e instale o plugin:

    ```
    /plugin marketplace add albertosca/vim-ai-follower
    /plugin install vim-ai-follower
    ```

2. Pro backend padrão `tmux`, pronto: ele não precisa de **nenhuma instalação via `pip`** — o plugin já empacota a CLI (só biblioteca padrão) e roda ela no lugar.
3. Pro backend `nvim`, instale também o pynvim no `python3` do seu `PATH`:

    ```sh
    pip install pynvim
    ```

Os hooks nunca derrubam uma chamada de ferramenta: eles são construídos para sair com `0` e registrar problemas em `~/.cache/claude-vim-follower/hook.log`, em vez de reportá-los ao Claude.

Próximo passo: [acompanhe sua primeira edição](first-follow.md).

## Instalação manual / de desenvolvimento

A partir de um clone — pra desenvolvimento, ou se você preferir não usar o plugin:

```sh
pip install -e .          # instala o script `claude-follow`
pip install -e '.[nvim]'  # adiciona o extra pynvim pro backend nvim
```

Depois conecte os hooks manualmente: adicione isto ao `~/.claude/settings.json` (use o caminho absoluto pro `claude-follow` instalado):

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Edit|MultiEdit|Write",
        "hooks": [{ "type": "command", "command": "claude-follow hook pre" }] }
    ],
    "PostToolUse": [
      { "matcher": "Edit|MultiEdit|Write",
        "hooks": [{ "type": "command", "command": "claude-follow hook post" }] },
      { "matcher": "Read",
        "hooks": [{ "type": "command", "command": "claude-follow hook post" }] }
    ]
  }
}
```
