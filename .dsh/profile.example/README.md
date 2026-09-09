# dsh profile template

This directory mirrors the profile shape documented by
`@deepseek-ai/dsh@0.1.5-alpha.1`: `package.json` carries `dsh.profile`, and
`cordis.patch.yml` carries the user patch layer. It is not an installed dsh
profile and is intentionally disabled by default.

To use it, copy the directory to the exact profile path reported by
`dsh --profile printops --dump-config` (normally
`$DSH_HOME/profiles/printops`), install the pinned dependency with pnpm, and
review the composed config before booting. Set a validated
`PRINTOPS_MCP_SESSION_ID` for the process, then remove `disabled: true` only
for a read-only smoke. The launcher still requires the concrete id and keeps
L1 disabled.

Do not commit a profile-local `node_modules` or replace the pinned version with
`latest`. The project-level skills remain in the repository's `.dsh/skills`;
the dsh filesystem skill provider discovers them from the invoking project
root.
