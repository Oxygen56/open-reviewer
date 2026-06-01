# Common Issues Encountered Across OSS PRs

Case studies and patterns observed across 35+ real open-source pull requests. These are organized by category for quick reference during review.

---

## Security Issues

### `data:` / `blob:` URL Injection (browser-use#4760)

**Problem:** A PR allowed user-supplied content to be rendered in a browser context via `data:` or `blob:` URLs without sanitization. An attacker could inject arbitrary JavaScript that executes in the extension's origin, bypassing CSP.

**Fix:** Validate that URLs use only `https:` or `http:` schemes when accepting user-supplied URLs for rendering. Block `data:`, `blob:`, `javascript:`, and `file:` schemes unless explicitly required.

**Detection:** Search for `data:` or `blob:` URL construction from user input. Look for `URL.createObjectURL` or `data:` URI generation without origin validation.

---

### Unsafe Subprocess Calls (lucebox-hub#316)

**Problem:** A PR used `subprocess.Popen(cmd, shell=True)` with a command string that included user-controlled variables. This allowed command injection via crafted input containing shell metacharacters.

**Fix:** Use `subprocess.Popen([cmd, arg1, arg2], shell=False)` with a command list to avoid shell interpretation. If `shell=True` is unavoidable, validate and sanitize all user inputs against a strict allowlist.

**Detection:** Search for `shell=True` in `subprocess` calls where any argument originates from user input, file paths, or external data.

---

### Unsafe Input to Exec/Eval (General)

**Problem:** Passing user-controlled strings to `eval()`, `exec()`, or `compile()` allows arbitrary code execution.

**Detection:** Search for `eval(`, `exec(`, `compile(`. These are almost never acceptable in production OSS code.

---

## GPU / CUDA Issues

### Shared Memory Overflow (DeepGEMM#322, DeepGEMM#339)

**Problem:** CUDA kernel launches specified shared memory sizes that exceeded the device's `maxSharedMemoryPerBlock` limit on certain architectures (e.g., sm_90a vs sm_90). The kernel compiled fine but failed at runtime on specific GPU generations.

**Fix:** Add device query at runtime (`cudaDeviceGetAttribute` for `cudaDevAttrMaxSharedMemoryPerBlock`) and clamp shared memory allocation, or provide kernel variants for different architecture tiers.

**Detection:** Look for `shared_mem` / `smem` / `__shared__` usage. Check if shared memory size accounts for the lowest-common-denominator device across supported compute capabilities.

---

### Python-CUDA Boundary Type Mismatch (FlashInfer#3176)

**Problem:** A Python wrapper passed `int` values to a CUDA kernel that expected `size_t` or `int64_t`. On 64-bit platforms this often worked, but on platforms where Python `int` defaulted to 32-bit, the kernel received truncated values, causing out-of-bounds access.

**Fix:** Always cast Python integer arguments to `ctypes.c_size_t` or the matching C type before passing to CUDA kernels. Use typed argument parsers in binding code (pybind11, ctypes, triton).

**Detection:** Examine binding code for integer type conversions between Python and C/CUDA. Look for implicit `int` passthrough without `ctypes` typing.

---

### Thread / Resource Double-Counting (llama.cpp#19110)

**Problem:** A PR added thread count tracking logic that incremented a shared counter on thread spawn and decremented on join. However, error paths and early exits bypassed the decrement, causing the counter to drift. Over multiple inference calls, the counter overflowed or triggered false-positive "all threads done" signals.

**Fix:** Use RAII wrappers (context managers, scoped guards) for resource counting. Ensure every increment path has a corresponding decrement in a `finally` block or destructor.

**Detection:** Look for manual increment/decrement patterns on shared counters across thread spawn/join boundaries. Check error-handling paths for missing cleanup.

---

## CI / DCO Issues

### DCO / Signed-off-by Missing (kserve#5608, kserve#5609)

**Problem:** Commits lacked a `Signed-off-by` trailer. Many CNCF and Linux Foundation projects require the Developer Certificate of Origin (DCO) on every commit. The PR was automatically blocked by a DCO check bot.

**Fix:** Run `git commit -s` (or `git rebase --signoff`) to add `Signed-off-by: Name <email>` to all commits. Verify with `git log --format="%H %s%n%b" | grep -c "Signed-off-by"`.

**Detection:** Pass `--check-signoff` to `gh pr view` or parse commit messages for `Signed-off-by:`.

---

### CLA Not Signed (google/adk-python#5916)

**Problem:** Google projects (and many corporate-backed OSS) require a signed Contributor License Agreement before accepting any PR. The contributor had not signed the CLA, so the CLA bot added a failing status check.

**Fix:** Sign the CLA via the link provided in the PR status check. This is a one-time action per organization.

**Detection:** Check CI status for "CLA" or "license" check failures. `gh pr view <PR> --json statusCheckRollup` reveals check names and states.

---

### Missing Dependencies in CI (camel-ai/camel#4083)

**Problem:** A PR added a new dependency (`pydantic`, `torch`, etc.) in the source code but did not update `setup.py`, `pyproject.toml`, or `requirements.txt`. CI passed because the dependency was installed transitively or existed in the test environment, but it failed for end users.

**Fix:** Update all relevant dependency declaration files. Run CI with a clean environment (e.g., `pip install -e .` without pre-installed extras).

**Detection:** Check for new `import` statements in the diff that are not reflected in dependency files. Look for `requirements/*.txt`, `setup.py`, `setup.cfg`, `pyproject.toml`.

---

### CI Flake vs. Real Failure (camel-ai/camel#4083)

**Problem:** A PR had a CI failure, but investigation revealed the same test was flaky on `main`. Maintainers still required the contributor to investigate, delaying the review.

**Fix:** Document flaky tests when you encounter them. Re-run CI to see if the failure is reproducible. Link to a pre-existing issue for the flaky test.

**Detection:** Note `statusCheckRollup` failures. Check the CI logs to see if the failure is in tests touched by the PR or unrelated tests.

---

## Dependency Management

### Undeclared New Dependency

**Problem:** A PR introduces `import requests` or `import torch` but never adds the package to `setup.py` / `pyproject.toml`.

**Detection:** Cross-reference new `import`/`require` statements in the diff against the project's dependency declaration files.

---

### Version Pin Mismatch

**Problem:** A PR pins a dependency to `>=X` when the rest of the project uses `>=X,<Y`. This can cause incompatible upgrades.

**Detection:** Check if the project uses constraints files or bounds in dependency declarations. Verify new pins match the project's convention.

---

### Transitive Dependency Conflict

**Problem:** A new dependency pulls in a transitive dependency whose version conflicts with an existing one. CI passes because the conflict is resolved silently, but runtime behavior is unpredictable.

**Detection:** Run `pip check` or equivalent after installing all dependencies.

---

## Protocol / API Design Issues

### Windows AF_UNIX Crash (skills#1120)

**Problem:** A PR used Unix domain sockets (AF_UNIX) for local IPC. This worked on Linux and macOS but crashed on Windows, where AF_UNIX socket support was only added in recent builds (Windows 10 build 17063) and is not available in older Windows Server versions.

**Fix:** Use a platform abstraction layer. On Windows, fall back to named pipes or TCP loopback. Alternatively, gate AF_UNIX usage behind a version check or feature flag.

**Detection:** Look for `socket.AF_UNIX`, `socket.socket(socket.AF_UNIX`, `unix:` in connection strings. Check if there's a platform branch or try/except for `AF_UNIX`.

---

### Deferred Tool Hook Race (SDK#993)

**Problem:** An SDK PR introduced deferred tool execution hooks. The hooks were registered on module import but executed by a lazy initializer. If two hooks tried to initialize the same resource concurrently (via `import` in separate threads), a race condition caused duplicate initialization or corrupted state.

**Fix:** Use `threading.Lock` or `threading.local()` for deferred initialization. Consider using `functools.cached_property` or a proper singleton pattern with double-checked locking.

**Detection:** Look for lazy initialization patterns, `@property` that initializes on first call, or import-time side effects. Check for thread safety in deferred execution paths.

---

### Wrong Base Branch

**Problem:** A PR targeting a repository that uses `develop` as the integration branch opened against `main` instead. The diff showed unexpected conflicts and the PR was closed without review.

**Fix:** Check the project's CONTRIBUTING.md or GitHub Actions workflow to identify the correct base branch. Rebase onto `develop` or `master` as appropriate.

**Detection:** Check PR metadata for `baseRefName`. Compare against the project's default branch and common workflows.

---

## General Coding Issues

### Large PR Scope Creep

**Problem:** A PR that started as a simple bugfix grew to include refactoring, formatting changes, and a new feature. Maintainers rejected the PR because the diff was too large to review confidently.

**Fix:** Split into multiple PRs: one for the bugfix, one for the refactoring, one for the feature. Keep each PR focused on a single concern.

**Detection:** Count lines changed. Flag PRs >500 lines or touching >10 files unless justified.

---

### Missing Tests for Bugfix

**Problem:** A PR fixing a bug did not include a regression test. Maintainers requested a test that reproduces the bug before the fix and passes after.

**Fix:** Always include a regression test with bugfix PRs. The test should fail on the old code and pass with the new code.

**Detection:** Check if the PR description says "bug" or "fix" but no test files are modified. Look for `test_` files in the diff.

---

### Commit Message Format Mismatch

**Problem:** The project enforces conventional commits (`type(scope): description`) but the PR commits used a non-standard format. The commit-lint CI check failed.

**Fix:** Use `git commit --amend` or `git rebase -i` to rewrite commit messages to match the project's convention. Common formats: `fix:`, `feat:`, `chore:`, `docs:`, `refactor:`, `test:`.

**Detection:** Parse commit messages against the project's expected format. Look for CI checks named "commit-lint", "semantic-pr", or similar.

---

## Quick Reference Checklist

| Check | Command / Method |
|-------|-----------------|
| DCO signoff | `git log --format="%H %s%n%b" \| grep "Signed-off-by"` |
| CLA status | `gh pr view <PR> --json statusCheckRollup` |
| CI results | `gh pr view <PR> --json statusCheckRollup` |
| Changed files | `gh pr diff <PR> --name-only` |
| Test changes | `gh pr diff <PR> --name-only \| grep -E '(test|spec)'` |
| Diff size | `gh pr diff <PR> \| wc -l` |
| Base branch | `gh pr view <PR> --json baseRefName` |
| Commits | `gh pr view <PR> --json commits` |
| PR description | `gh pr view <PR> --json body` |
