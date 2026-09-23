🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/reference/config/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/reference/config/)

# Configuração

JSON opcional em `~/.config/claude-vim-follower/config.json`. Toda chave tem um padrão, e um valor inválido cai de volta pro padrão em silêncio. Volte para o [README](https://github.com/albertosca/vim-ai-follower/blob/main/README.pt.md).

## Chaves de configuração

| Chave            | Valores                                                    | Padrão     | Significado                                                                             |
| ---------------- | ----------------------------------------------------------- | ---------- | ---------------------------------------------------------------------------------------- |
| `backend`        | `tmux`, `nvim`                                              | `tmux`     | `tmux`: controla um Vim sem modificações num pane de tmux via `send-keys`. `nvim`: controla um Neovim de verdade via msgpack-RPC (adota um nvim rodando na janela, ou lança um dedicado — veja [Backend Neovim](../guides/nvim-backend.md)). Requer o extra de instalação `nvim`. |
| `open_policy`    | `always`, `code`, `manual`                                  | `manual`   | `manual`: só o `start` abre um follower. `code`: abre automaticamente em edições/leituras de arquivos de código; arquivos não-código também são ignorados (nunca animados), mesmo com um follower iniciado manualmente. `always`: abre automaticamente em qualquer arquivo. |
| `adopt_existing` | `true`, `false`                                             | `false`    | Ao abrir automaticamente (ou no `start`), reaproveita um Vim já rodando na janela em vez de abrir um pane novo. Veja [Adotando um editor](../guides/adopting-an-editor.md). |
| `max_tabs`       | inteiro ≥ 1                                                 | `5`        | Quantas abas de arquivo o follower mantém. A aba menos usada recentemente é fechada ao passar desse limite. Veja [Abas multi-arquivo](../guides/multi-file-tabs.md). |
| `on_failure`     | `silent`, `reopen`                                          | `silent`   | Se o pane do follower morrer, `reopen` abre um novo a partir de onde começou; `silent` só para de acompanhar. |
| `speed`          | `instant`, `muito_rapido`, `rapido`, `normal`, `lento`      | `rapido`   | Ritmo da animação (segundos por quebra de linha: `0`, `0.01`, `0.03`, `0.08`, `0.15`).    |

## Exemplo

```json
{
  "backend": "nvim",
  "open_policy": "code",
  "max_tabs": 8,
  "speed": "normal"
}
```

Flags passadas ao [`claude-follow start`](cli.md) sobrescrevem `backend`, `on_failure` e `speed` pra aquela sessão.
