# Devnet Release Checklist

## Code frozen

- reward/protocol logic frozen
- only release docs, metadata, or blocker fixes after this point

## Tests green

- CLI reward diagnostics tests green
- HTTP reward diagnostics tests green
- snapshot tests green
- DB bootstrap tests green

## Snapshot ready

- fresh devnet snapshot exported from upgraded canonical node
- snapshot import verified on a clean host
- snapshot signature verified if trust mode is enforced operationally

## Upgrade rehearsal done

- in-place upgrade rehearsed on `chipcom`
- in-place upgrade rehearsed on `tilt`
- in-place upgrade rehearsed on `tobia`
- post-upgrade reward diagnostics verified on all three

## Bootstrap rehearsal done

- one fresh node bootstrapped from zero
- first sync verified
- first reward diagnostics verified

## Docs updated

- release notes updated
- host upgrade runbook updated
- fresh-node bootstrap runbook updated

## Deployment executed

- release branch or commit pushed
- `chipcom` upgraded
- `tilt` upgraded
- `tobia` upgraded

## Post-deploy verification complete

- all three hosts agree on tip hash
- reward diagnostics agree across CLI and HTTP
- fresh snapshot exported after deploy
