set positional-arguments

[private]
@default:
    {{quote(just_executable())}} --list

# Save a private OpenTofu plan for review (create or managed)
plan PROFILE INTENT:
    #!/usr/bin/env bash
    set -euo pipefail

    refuse() {
      printf '%s\n' "$1" >&2
      exit 1
    }

    PROFILE=$1
    INTENT=$2
    case "$PROFILE" in
      pilot-74)
        CLUSTER=perfeng-iac-pilot-74
        STATE_DIR="$HOME/.local/state/perfeng/kind-74"
        ;;
      retained-434)
        CLUSTER=perfeng-retained-434
        STATE_DIR="$HOME/.local/state/perfeng/kind-434"
        ;;
      *) refuse 'Unknown kind profile. Choose pilot-74 or retained-434.' ;;
    esac
    case "$INTENT" in
      create|managed) ;;
      *) refuse 'Plan intent must be create or managed.' ;;
    esac

    SOURCE_REVISION=$(git rev-parse --verify 'HEAD^{commit}')
    CHECKOUT_STATUS=$(git status --porcelain=v1 --untracked-files=all --ignore-submodules=none)
    [[ -z "$CHECKOUT_STATUS" && "$(git rev-parse --verify 'HEAD^{commit}')" == "$SOURCE_REVISION" ]] ||
      refuse 'Plan from a clean, reviewed checkout at a stable HEAD.'

    export PATH="$HOME/.local/state/perfeng/kind-74/bin:$PATH"
    export DOCKER_HOST=unix:///var/run/docker.sock KIND_EXPERIMENTAL_PROVIDER=docker
    export TF_CLI_CONFIG_FILE="$STATE_DIR/opentofu.tfrc"
    unset DOCKER_CONTEXT TF_WORKSPACE TF_DATA_DIR TF_CLI_ARGS TF_CLI_ARGS_init \
      TF_CLI_ARGS_state TF_CLI_ARGS_console TF_CLI_ARGS_plan TF_CLI_ARGS_show
    umask 077

    LOCK="$STATE_DIR/operation.lock"
    [[ -d "$STATE_DIR" && "$(realpath -e -- "$STATE_DIR")" == "$STATE_DIR" &&
       ! -L "$LOCK" && ( ! -e "$LOCK" || -f "$LOCK" ) ]] ||
      refuse 'The registered state directory or lock path is missing or redirected.'
    exec 9>>"$LOCK"
    flock -w 15 9 || refuse 'The target lock is busy. Use the runbook recovery steps.'

    STATE="$STATE_DIR/cluster.tfstate"
    PLANS="$STATE_DIR/plans"
    KCFG="$STATE_DIR/kubeconfig"
    IDENTITY="$STATE_DIR/identity.json"
    CTX="kind-$CLUSTER"
    [[ ! -L "$STATE" && ( ! -e "$STATE" || -f "$STATE" ) &&
       ! -L "$PLANS" && ( ! -e "$PLANS" || -d "$PLANS" ) ]] ||
      refuse 'The state file or plan directory is invalid or redirected.'
    [[ -f "$TF_CLI_CONFIG_FILE" && ! -L "$TF_CLI_CONFIG_FILE" ]] ||
      refuse 'The registered OpenTofu CLI configuration is missing or redirected.'

    if [[ "$INTENT" == managed ]]; then
      [[ -f "$KCFG" && ! -L "$KCFG" && -f "$IDENTITY" && ! -L "$IDENTITY" ]] ||
        refuse 'The kubeconfig or identity record is missing or redirected.'
      EXPECTED_DAEMON=$(jq -er '.daemon | strings | select(length > 0)' "$IDENTITY") ||
        refuse 'The identity record has no valid Docker daemon ID.'
      EXPECTED_UID=$(jq -er '.uid | strings | select(length > 0)' "$IDENTITY") ||
        refuse 'The identity record has no valid kube-system UID.'
    else
      : "${EXPECTED_DAEMON:?Set the registered Docker daemon ID}"
      [[ ! -e "$KCFG" && ! -L "$KCFG" && ! -e "$IDENTITY" && ! -L "$IDENTITY" ]] ||
        refuse 'The selected target already has a registration file.'
    fi

    DAEMON=$(timeout 15s docker --host "$DOCKER_HOST" info --format '{{{{.ID}}')
    [[ "$DAEMON" == "$EXPECTED_DAEMON" ]] || refuse 'Docker daemon ID differs from the registration.'
    if [[ "$INTENT" == create ]]; then
      CLUSTER_LABELS=$(timeout 15s docker --host "$DOCKER_HOST" ps -a \
        --filter "label=io.x-k8s.kind.cluster=$CLUSTER" --format '{{{{.Names}}')
      CLUSTER_NAMES=$(timeout 15s docker --host "$DOCKER_HOST" ps -a \
        --filter "name=^/$CLUSTER-" --format '{{{{.Names}}')
      [[ -z "$CLUSTER_LABELS$CLUSTER_NAMES" ]] || refuse 'The selected kind name is already in use.'
    fi

    # Archive only the tracked inputs belonging to this profile.
    SOURCE=clusters/local-kind/iac-pilot
    PROFILE_SOURCE="$SOURCE/profiles/$PROFILE"
    INPUTS=("$SOURCE/main.tf" "$SOURCE/.terraform.lock.hcl" "$PROFILE_SOURCE/target.auto.tfvars")
    if [[ "$PROFILE" == retained-434 ]]; then
      INPUTS+=("$PROFILE_SOURCE/retained-lifecycle.tf")
    fi
    install -d -m 700 "$PLANS"
    PLAN_DIR=$(mktemp -d "$PLANS/plan.XXXXXX")
    git archive --format=tar "$SOURCE_REVISION" -- "${INPUTS[@]}" |
      tar -xf - --strip-components=3 --no-same-owner --no-same-permissions -C "$PLAN_DIR"
    for INPUT in "${INPUTS[@]}"; do
      STAGED="$PLAN_DIR/${INPUT#"$SOURCE/"}"
      [[ -f "$STAGED" && ! -L "$STAGED" ]] || refuse 'A reviewed input is missing or redirected.'
      chmod 600 -- "$STAGED"
    done
    mv -- "$PLAN_DIR/profiles/$PROFILE/target.auto.tfvars" "$PLAN_DIR/target.auto.tfvars"
    if [[ "$PROFILE" == retained-434 ]]; then
      mv -- "$PLAN_DIR/profiles/$PROFILE/retained-lifecycle.tf" "$PLAN_DIR/retained_override.tf"
    fi
    rmdir -- "$PLAN_DIR/profiles/$PROFILE" "$PLAN_DIR/profiles"
    printf '%s\n' "$SOURCE_REVISION" > "$PLAN_DIR/source-revision.txt"
    export KUBECONFIG="$PLAN_DIR/provider.kubeconfig"
    tofu -chdir="$PLAN_DIR" init -no-color -input=false -lockfile=readonly -backend-config="path=$STATE"

    if [[ "$INTENT" == managed ]]; then
      STATE_RESOURCES=$(tofu -chdir="$PLAN_DIR" state list)
      [[ "$STATE_RESOURCES" == kind_cluster.pilot ]] || refuse 'The registered state must contain only kind_cluster.pilot.'
      NAME_MATCH=$(tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE" \
        <<< "kind_cluster.pilot.name == var.cluster_name && var.cluster_name == \"$CLUSTER\"")
      [[ "$NAME_MATCH" == true ]] || refuse 'The state or staged cluster name differs from the registration.'
      kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
        wait --for="jsonpath={.metadata.uid}=$EXPECTED_UID" --timeout=1s namespace/kube-system ||
        refuse 'The live kube-system UID could not be matched to the registration.'
    else
      if [[ -e "$STATE" ]]; then
        STATE_RESOURCES=$(tofu -chdir="$PLAN_DIR" state list)
        [[ -z "$STATE_RESOURCES" ]] || refuse 'The selected state already contains resources.'
      fi
      NAME_MATCH=$(tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE" <<< "var.cluster_name == \"$CLUSTER\"")
      [[ "$NAME_MATCH" == true ]] || refuse 'The staged cluster name differs from the registration.'
    fi

    PLAN="$PLAN_DIR/saved.tfplan"
    tofu -chdir="$PLAN_DIR" plan -no-color -input=false -lock-timeout=30s -out="$PLAN" 2>&1 |
      tee "$PLAN_DIR/plan.log"
    tofu -chdir="$PLAN_DIR" show -no-color "$PLAN" > "$PLAN_DIR/plan.txt"
    sha256sum -- "$PLAN" > "$PLAN_DIR/SHA256SUMS"
    read -r PLAN_SHA256 _ < "$PLAN_DIR/SHA256SUMS"
    cat "$PLAN_DIR/plan.txt" "$PLAN_DIR/SHA256SUMS"
    printf '# Plan review\n\n- Profile %s\n- Intent %s\n- Source revision %s\n- Plan %s\n- SHA-256 %s\n' \
      "$PROFILE" "$INTENT" "$SOURCE_REVISION" "$PLAN" "${PLAN_SHA256#\\}" > "$PLAN_DIR/REVIEW.md.tmp"
    mv -- "$PLAN_DIR/REVIEW.md.tmp" "$PLAN_DIR/REVIEW.md"
