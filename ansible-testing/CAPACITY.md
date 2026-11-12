# Ansible Island Adventure — Capacity Planning

Last updated: 2026-09-08
Cluster: OCP 4.21 on CNV (`ocp_cloud_provider: cnv`)
Measured on: `cluster-dg7nm` (5 workers × 64 vCPU / 128Gi)

## Per-Worker Specs

| Parameter | Value |
|-----------|-------|
| Instance type | m5a (CNV equivalent) |
| vCPU per worker | 64 |
| RAM per worker | 128Gi |
| Allocatable CPU | 63.18 vCPU (after kubelet reservation) |
| Allocatable RAM | 120.5Gi |

## Shared Services (fixed cost, independent of instructor count)

Measured resource **requests** from a live 2-instructor deployment:

| Service | Pods | CPU Request | Memory Request | Notes |
|---------|------|-------------|----------------|-------|
| AAP 2.7 (Gateway + Controller) | 16 | 1,490m | 5,152Mi | Single shared instance |
| OpenShift Virtualization | 55 | 1,505m | 8,286Mi | CNV operator + virt components |
| Dev Spaces | 4 | 650m | 1,376Mi | CheCluster (workspaces spawn on demand) |
| Gitea + PostgreSQL | 2 | 300m | 512Mi | Shared Git hosting |
| **Shared total** | **77** | **3,945m** | **15,326Mi (15Gi)** | |

OCP system overhead (monitoring, routers, image-registry, OLM, DNS, etc.):
~8,000m CPU, ~30Gi RAM estimated.

**Total fixed overhead: ~12 vCPU, ~45Gi RAM**

## Per-Instructor Cost

Each instructor gets:

| Component | Pods | CPU Request | Memory Request |
|-----------|------|-------------|----------------|
| MariaDB | 1 | 100m | 256Mi |
| Redis | 1 | 50m | 128Mi |
| CTFd | 1 | 200m | 256Mi |
| Verifier | 1 | 50m | 64Mi |
| RHEL9 VM (virt-launcher) | 1 | 1,000m + 100m overhead | 2,048Mi + 268Mi overhead |
| **Per-instructor total** | **5** | **1,500m** | **3,020Mi (~3Gi)** |

Notes:
- The VM uses 1 vCPU and 2Gi guest memory; the virt-launcher pod adds ~100m/268Mi overhead.
- CTFd resource requests are lab-optimized (small requests, higher limits).
- Each VM requires a 30Gi PVC (RHEL9 image clone from DataSource).

## Storage Per Instructor

| Volume | Size | Access Mode |
|--------|------|-------------|
| VM root disk (DataVolume from rhel9 snapshot) | 30Gi | RWO |
| MariaDB PVC | 1Gi | RWO |
| Redis PVC | 1Gi | RWO |
| CTFd logs PVC | 1Gi | RWO |
| CTFd uploads PVC | 1Gi | RWO |
| **Per-instructor storage** | **34Gi** | |

## Capacity Calculation

### Available resources for instructors

```
Total allocatable (5 workers):
  CPU:  63,180m × 5 = 315,900m
  RAM:  120.5Gi × 5 = 602.5Gi

Minus shared + system overhead:
  CPU:  315,900m - 3,945m - 8,000m = 303,955m
  RAM:  602.5Gi - 15Gi - 30Gi      = 557.5Gi

Available for instructors:
  CPU:  ~304 vCPU
  RAM:  ~558Gi
```

### Instructors per cluster

| Workers | Allocatable CPU | Allocatable RAM | Max Instructors (CPU) | Max Instructors (RAM) | Limiting Factor | With 25% Headroom |
|---------|----------------|-----------------|----------------------|----------------------|-----------------|-------------------|
| 3 | 189.5 vCPU | 361.5Gi | 115 | 105 | RAM | **79** |
| 4 | 252.7 vCPU | 482Gi | 157 | 145 | RAM | **109** |
| **5** | **315.9 vCPU** | **602.5Gi** | **198** | **185** | **RAM** | **139** |
| 6 | 379.1 vCPU | 723Gi | 240 | 226 | RAM | 170 |
| 8 | 505.4 vCPU | 964Gi | 326 | 306 | RAM | 230 |
| 10 | 631.8 vCPU | 1,205Gi | 410 | 386 | RAM | 290 |

*Max Instructors = (Allocatable - Shared - System) ÷ Per-Instructor cost*
*Headroom = Max × 0.75 (25% buffer for burst, scheduling overhead, and OCP upgrades)*

### Storage at scale

| Instructors | VM Disks | Other PVCs | Total Storage |
|-------------|----------|------------|---------------|
| 50 | 1,500Gi | 200Gi | 1,700Gi |
| 100 | 3,000Gi | 400Gi | **3,400Gi** |
| 200 | 6,000Gi | 800Gi | 6,800Gi |

Ensure the Ceph/ODF storage pool has sufficient capacity.

## Recommendation: 100 Instructors

### Target: 5 workers × 64 vCPU / 128Gi

| Metric | Value | At 100 Instructors | Utilization |
|--------|-------|-------------------|-------------|
| CPU available | 304 vCPU | 150 vCPU used | **49%** |
| RAM available | 558Gi | 300Gi used | **54%** |
| Storage | Cluster dependent | 3,400Gi needed | Check ODF pool |
| Namespaces | — | ~203 (2 per instructor + shared) | |
| Pods | — | ~580 (5 per instructor + 77 shared) | |
| Routes | — | ~202 (2 per instructor + shared) | |

**5 workers is the right size for 100 instructors.** At ~50% utilization,
there is comfortable headroom for:
- VM burst (guest workloads spiking during challenges)
- Dev Spaces workspace launches (each workspace adds ~500m/1Gi on demand)
- Pod scheduling imbalance across nodes
- Unexpected operator restarts or rolling updates

Going below 5 workers (e.g., 4) puts you at 73% utilization, which is
workable but leaves less room for burst. Going above (e.g., 6) gives more
headroom but is unnecessary spend.

### RHDP Provisioning Parameters

```yaml
ocp_cloud_provider: cnv
cluster_size: multinode
create_multi_user: true
num_users_param: 100
worker_instance_count_param: 5
host_ocp4_installer_version: "4.21"
ai_workers_cores: 64
ai_workers_memory: 128Gi
```

### Deploy Command

```bash
cd deploy
ansible-playbook deploy.yml
# Uses all 100 users from .cnv.env by default (instructor_users: [])
```

## Scaling Beyond 100

| Target | Workers | Clusters |
|--------|---------|----------|
| 100 instructors | 5 | 1 |
| 200 instructors | 8–10 | 1 |
| 300 instructors | 10 | 1 (tight) or 2 (comfortable) |
| 500 instructors | 10 | 2 |

For multi-cluster deployments, each cluster runs independently with its
own `.cnv.env` and `deploy.yml` invocation. No cross-cluster coordination
is needed — each cluster is a self-contained island.
