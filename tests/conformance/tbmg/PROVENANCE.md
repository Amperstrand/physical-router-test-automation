# Vendored tbmg assets — provenance

Byte-exact copies from the **Amperstrand fork** of
tollgate-module-basic-go (`fork/main`). Do not hand-edit; refresh with:

```bash
cd <tbmg-checkout>
git show fork/main:packaging/files/etc/init.d/tollgate-wrt \
  > tests/conformance/tbmg/init.d.tollgate-wrt
```

| File | Source | Why vendored |
|---|---|---|
| `init.d.tollgate-wrt` | `packaging/files/etc/init.d/tollgate-wrt` (fork, PR #98 merged) | The `stop_service()` stray-`:2121` sweep the O3 rail asserts against. Not yet in upstream main; vendoring keeps the PRTA mirror runnable everywhere. |
| `min_steps_table.json` | rows of `src/config_manager/min_steps_test.go` (fork) | The parser-contract table the O4 rail pins. |

`test_stray_2121_sweep.py` / `test_tbmg_parser_contract.py` assert —
when `TBMG_CHECKOUT` points at a tbmg clone — that the vendored copies
have not drifted from the fork. Set `TBMG_CHECKOUT=/path/to/tollgate-module-basic-go`.
