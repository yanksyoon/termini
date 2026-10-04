# Launching the agent VM image (reference)

This is the reference launch used to prove the image; **distribution to Nodes A/B
and project-SSH-key injection are `t_7c40f9a3`'s scope** (it owns the `agent-os/w4`
alias + placement). Values below come from `docs/contracts/agent-runtime.md`
§4.2 and §19.1.

## Alias produced by this card

```
agent-vm/2026.10.0            # stable alias
agent-vm-2026.10.0+g<gitsha>  # immutable tag
```

Export / import a tarball for another node:

```bash
lxc image export agent-vm/2026.10.0 ./agent-vm-2026.10.0     # -> ./agent-vm-2026.10.0.tar.gz
lxc image import ./agent-vm-2026.10.0.tar.gz --alias agent-os/w4   # on Node A/B
```

## Online launch (contract §4.2 shape)

```bash
lxc launch agent-vm/2026.10.0 agent-01H4KZ9Q \
  -c limits.cpu=2 -c limits.memory=4GiB \
  -d root,size=8GiB \
  --config cloud-init.user-data="$(cat /var/lib/termini/agent-user-data.yaml)"
```

`/etc/termini/agent.yaml` is rendered by cloud-init at launch (contract §4.4);
the image ships a baked default so the runtime is alive before cloud-init runs.

## Offline-boot check (what the acceptance uses)

`termini-agent-offline` is a profile with a root disk and **no NIC**:

```bash
lxc profile create termini-agent-offline
lxc profile device add termini-agent-offline root disk path=/ pool=default
lxc launch agent-vm/2026.10.0 agent-offline -p termini-agent-offline
lxc exec agent-offline -- bash /root/tools/offline-acceptance.sh
```
