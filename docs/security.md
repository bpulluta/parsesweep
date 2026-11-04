# Security and protected operation

## Status and boundaries

This is a hardened application, **not an authorization to process sensitive
data** and not a claim of zero vulnerabilities, FIPS validation, compliance,
or HPC certification. Python 3.12/macOS ARM64 is the locally executed baseline;
other locked platforms require execution on their target systems.

The refreshed Python audit still reports two advisories against
`filelock 3.20.3` (CVE-2026-112909 and CVE-2026-112910). This compatible
release avoids later 3.x lock regressions. The fixed release is `4.2.1`, but the current
LiteLLM dependency constrains it below 4. No incompatible override or advisory
suppression is used. Many findings require an attacker-writable local lock/cache
directory; that limits applicability, not the audit result. Keep the release
gate closed pending an upstream-compatible fix or a formal site exception.
Native libraries were refreshed, not comprehensively vulnerability-scanned.

### Trust model

* URLs, documents, OCR, provider responses, filenames, and spreadsheet evidence
  are untrusted. Configurations, schemas, installed code, the OS trust store,
  and the operator-approved model endpoint are administrative inputs.
* Public discovery connects only to globally routable addresses. Every DNS
  answer is checked; the validated sockaddr is used directly, with original
  hostname verification/SNI. Redirects and robots lookups retain source gates.
  Proxies, `.netrc`, and URL credentials are not accepted.
* Do not place API keys or secrets in discovery URL queries or custom headers.
  Source URLs are provenance and can appear in manifests and diagnostics.
* TLS requires at least 1.2 and defaults to authenticated certificates. System roots are
  the default; `REQUESTS_CA_BUNDLE` or `SSL_CERT_FILE` selects approved roots.
  Public mode permits an explicitly warned `PSWEEP_SSL_VERIFY=false` (models
  and search/download fallback), `SERPAPI_SSL_VERIFY=false`, or
  `DISCOVERY_SSL_VERIFY=false`. Connections and credentials can then be
  intercepted: this is compatibility operation, not secure production use.
  Protected mode rejects every configured false setting. Invalid settings
  fail in both modes. Stage-specific settings override the global fallback;
  explicit request arguments override those settings in public mode.
  Disabling verification never relaxes public-address or redirect checks.
* Downloads count decoded bytes (default 50 MiB; positive integer
  `PSWEEP_MAX_RESPONSE_BYTES`). The configured request timeout bounds socket
  waits and response elapsed-time checks. It is **not a hard whole-job deadline**:
  DNS, native parsing, decompression internals, and OS scheduling need external
  resource controls.
* Ordinary browser operation is disabled. Public browser use requires
  `PSWEEP_BROWSER_ISOLATED=1` and an executable `PSWEEP_CHROMEDRIVER`.
  This is an operator attestation, not automatic sandbox/network validation.
  Chrome's sandbox stays enabled; drivers are never downloaded at runtime.
  Enforce network restrictions outside the browser, including DNS, subresources,
  redirects, and downloads. Browsers are forbidden in protected mode.
* Model output is data, never a command. Prompt injection can still change
  extraction accuracy; schemas and review are not proof of factual correctness.
  No model/tool-execution permission is granted by document content.
* CSV exports prefix formula-like strings with an apostrophe; numeric values
  remain numeric. XLSX evidence cells remain strings. Authored QA/QC formulas
  escape model labels, and dropdowns reference literal cells. Canonical JSON
  retains original evidence. CSV machine ledgers are not spreadsheet reports:
  import them as text rather than opening them with spreadsheet auto-detection.

## Public acquisition and sensitive processing are separate jobs

Acquire and quarantine public documents on a public-data system first. Scan,
inspect, and transfer approved input into a restricted workspace. Do not allow
public discovery or browser execution from a sensitive-data compute allocation.

Protected CLI operation:

1. Provision an owner-only POSIX work directory (`chmod 700`) below trusted,
   non-group/world-writable ancestors, or a suitable sticky scratch directory.
   Verify ACLs, ownership, encryption, quotas, and cleanup policy separately.
2. Set `PSWEEP_SECURITY_MODE=protected`. Set `LLM_BASE_URL` to the exact approved
   HTTPS OpenAI-compatible endpoint and supply its credential securely through
   the environment. Direct providers and differing per-stage endpoints fail.
3. Set `REQUESTS_CA_BUNDLE` only if a site-approved private CA is needed.
   Protected model requests disable redirects, environment proxies, provider
   plugins, and callbacks. They do not fall back to another provider.
4. Run from the private workspace. Input and output directories configured for
   stages must remain inside it. Automatic dotenv loading is disabled.
   Private caches and temporaries are placed below `.psweep-private/`; inherited
   cache/TMPDIR overrides outside the workspace are rejected. Library API users
   must invoke `prepare_protected_runtime()` before creating data or caches.
5. Disable discovery in the run configuration (or use `--skip-discover`) and
   explicitly configure local extraction inputs. Protected compilation requires
   canonical records and fails on provenance warnings.

Example (synthetic placeholders, never literal credentials):

```bash
cd /approved/private/job-workspace
export PSWEEP_SECURITY_MODE=protected
export LLM_BASE_URL=https://approved-model.example/v1
# Inject LLM_API_KEY using your site's credential mechanism.
unset HF_HOME HUGGINGFACE_HUB_CACHE TIKTOKEN_CACHE_DIR XDG_CACHE_HOME TMPDIR
psweep run --config run.yaml --skip-discover --yes
```

The application selects an endpoint; only firewall/namespace rules can prove
egress confinement. DNS changes at the trusted endpoint, administrative
configuration changes, compromised dependencies, native parser exploits,
and inherited host privileges remain outside that guarantee.

## HPC operating checklist

Before any real sensitive workload, the site owner must verify:

- [ ] Institutional data classification, model/provider authorization, retention,
      training/logging policy, geography, contractual terms, and access controls.
- [ ] Unprivileged UID, dedicated private workspace, protected ancestors, ACLs,
      encrypted storage/scratch where required, and no shared writable caches.
- [ ] Network namespace/firewall egress only to approved inference/DNS services;
      no public internet, metadata service, browser, or credentialed proxy path.
- [ ] Immutable reviewed image/environment, native-library SBOM and advisory
      scan, dependency provenance, and explicit handling of the filelock blocker.
- [ ] Scheduler/cgroup CPU, memory, process, file-descriptor, file-size, and wall
      limits. Native PDF/OCR work needs OS isolation, not only Python timeouts.
- [ ] Separate read-only input mount and bounded output/temp mounts; parser
      isolation with restricted syscalls/privileges appropriate to the site.
- [ ] Synthetic extraction and compilation on the actual scheduler and
      filesystem, including cancellation, OOM, full disk, restart, and recovery.
- [ ] Lock behavior on actual NFS/Lustre/GPFS configuration; one writer per
      output directory, no lock-inode removal, and read-after-success consumers.
- [ ] Model accuracy, prompt-injection review, completeness, throughput and
      cost measured with representative approved data; humans adjudicate output.
- [ ] Backup/recovery, retention deletion, incident response, access audit,
      and log redaction verified without exporting sensitive diagnostics.

Compilation and discovery journals recover interrupted *publication* on the
next locked writer. They are not whole-pipeline transactions or atomic
multi-file snapshots for concurrent readers. Power-loss durability and
distributed filesystem semantics need site testing.

## Migration and validation

Provision approved VPN/enterprise CA trust rather than disabling verification.
Remove false SSL_VERIFY settings before protected operation. Public opt-out is
explicit, warned, and scoped to each request/client, not a global SDK mutation.
Private discovery URLs no longer work; ingest approved private
documents locally. Browser users must provision isolated infrastructure and a
driver explicitly. Python 3.9/3.10/3.11 are no longer supported.

Nested compilation rows are selected from the schema. One nested object array
expands automatically; multiple candidates need
`$metadata.compilation.flattening.row_array` naming the desired field.
Use explicit `null` to retain child arrays as columns. Parent/child column
collisions fail rather than silently overwrite. Malformed payloads never
publish; public `--provenance-policy warn` can inspect incomplete records, but
release/protected runs require the canonical extraction-record contract.
Protected library compilation also rejects provenance warnings and data paths
outside the private workspace. Missing extraction records fail without changing
existing exports. Valid zero-item `--fresh` compilation clears stale output
through the same locked, recoverable publication path as nonempty output.

Run ordinary tests offline; `-m integration` includes actual local TLS/provider
requests with synthetic data. `-m live --live` explicitly enables approved
public endpoint smoke tests. Also execute the real subprocess CLI pipeline,
verify unchanged extraction reuse and output values, build documentation and a
wheel, install the wheel cleanly, and audit the installed release inventory.
Mocked requests, resolved lockfiles, or an HTTP 200 alone do not satisfy these
acceptance gates.
