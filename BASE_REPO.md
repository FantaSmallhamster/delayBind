# ReMemR1 Baseline

V5 is built on the upstream ReMemR1 repository and keeps its original
recurrent agent, training, evaluation, and baseline implementations intact.

- Upstream: https://github.com/syr-cn/ReMemR1
- Locked commit: `cc514c092ca968a50c52cdcc2e2ba96362fce25a`
- Checkout: detached at the locked commit
- V5 additions: `delaybind_core/` and (later) a separate recurrent adapter

Do not update the upstream baseline in place. Record any intentional upstream
change in `PATCH_NOTES.md` and keep the baseline runnable for comparison.
