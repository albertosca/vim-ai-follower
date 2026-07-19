"""Lua dispatched into the follower nvim over nvim_exec_lua. Kept as string
constants (not an installed plugin): the char loop paces with vim.wait so nvim
stays responsive, highlights the typed line with an extmark, and moves the
cursor. Args: (bufnr, row, text, pace_ms, ns_id)."""

TYPE_LINE = r"""
local bufnr, row, text, pace_ms, ns = ...
vim.api.nvim_buf_set_lines(bufnr, row, row, true, { "" })
local mark = vim.api.nvim_buf_set_extmark(bufnr, ns, row, 0, { hl_eol = true, hl_group = "Visual", end_row = row + 1, end_col = 0 })
for i = 1, #text do
  vim.api.nvim_buf_set_text(bufnr, row, i - 1, row, i - 1, { text:sub(i, i) })
  pcall(vim.api.nvim_win_set_cursor, 0, { row + 1, i })
  if pace_ms > 0 then vim.wait(pace_ms) end
end
vim.api.nvim_buf_del_extmark(bufnr, ns, mark)
return true
"""
