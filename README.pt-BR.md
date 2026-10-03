<a id="top"></a>
# lab-in-a-box

<p align="center">
  <a href="https://rmahique.github.io/lab-in-a-box/logo/">
    <img src="media/brand/logo-3d-4f.svg" width="240" alt="Logotipo do lab-in-a-box: cubos brilhantes aninhados dentro de uma caixa de vidro, representando VMs aninhadas dentro de uma máquina física" />
  </a>
  <br/>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="media/brand/logo-text-dark.png">
    <img src="media/brand/logo-text.png" width="420" alt="Wordmark do lab-in-a-box" />
  </picture>
</p>

<p align="center">
  <a href="https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Licença" src="https://img.shields.io/badge/license-GPLv3-12a99d.svg">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11-1f2f4a.svg">
  <img alt="Tests" src="https://img.shields.io/badge/tests-containerized%20(podman)-0f8f86.svg">
  <img alt="Add-ons" src="https://img.shields.io/badge/add--ons-61-e2725b.svg">
</p>

<p align="center">
  <sub>🌐 <a href="README.md">English</a> · <a href="README.es.md">Español</a> · <a href="README.de.md">Deutsch</a> · <a href="README.fr.md">Français</a> · <a href="README.pt-BR.md"><strong>Português (Brasil)</strong></a> · <a href="README.ja.md">日本語</a> · <a href="README.zh-CN.md">简体中文</a></sub>
</p>

> *Esta é uma tradução da comunidade. A fonte de referência é o [README.md](README.md) (inglês) e pode estar mais atualizada do que esta página.*

<p align="center"><em>Aponte para um arquivo JSON ou YAML. Receba de volta um laboratório funcionando — VMs, DNS, Kubernetes e add-ons, tudo conectado.</em></p>

<p align="center" float="left">
  <kbd><img src="media/NUC.jpg" width="400" alt="Um dos NUCs usados para desenvolver e testar este projeto." /></kbd>
</p>

**lab-in-a-box** transforma uma única máquina física em uma fábrica de laboratórios autocontida: aponte-o para um arquivo JSON ou YAML descrevendo as VMs, os clusters Kubernetes e o software desejados, e ele constrói tudo — DNS, provisionamento, subida do cluster e add-ons — sem que você precise mexer no `virt-install` ou no Ansible manualmente.

## Por que lab-in-a-box?

<table>
<tr>
<td width="50%" valign="top">

`setup_lab.py` · **Um arquivo JSON/YAML, um comando.**
Descreva VMs, clusters Kubernetes (RKE2/K3s) e add-ons de forma declarativa; ele constrói tudo na ordem correta.

`install_<addon>` · **65 add-ons prontos para uso.**
Rancher, Longhorn, NeuVector, Harbor, Keycloak, Jenkins, Argo CD, SUSE Multi-Linux Manager/Uyuni (chaves de ativação, RBAC, Content Lifecycle Management, integração com Ansible, e mais), aplicações de demonstração vulneráveis para treinamento de segurança, e mais.

[`lab-builder`](#web-ui-lab-builder) · **Uma interface web dinâmica.**
Gera formulários diretamente a partir do próprio esquema de cada add-on — adicione um campo a um script, e a interface o reconhece sem nenhuma mudança no frontend.

</td>
<td width="50%" valign="top">

`KVM_HOSTS` · **Consciente de múltiplos hipervisores.**
Uma única definição de laboratório pode distribuir VMs entre vários hosts KVM, selecionados automaticamente por CPU/RAM/disco livres, ou fixados por nó.

`podman` · **Suíte de testes totalmente containerizada.**
Cada verificação roda em seu próprio container descartável, integrada a um hook de pre-commit.

`config_method` · **Provisionamento plugável.**
Ignition+Combustion (SLE Micro), cloud-init (openSUSE/Ubuntu), `virt-customize` (distribuições antigas sem suporte a cloud-init/Ignition), ou uma instalação via ISO com script (AutoYaST/Kickstart/Preseed/AutoInstall).

</td>
</tr>
</table>

---

## Sumário

- [Arquitetura](#architecture)
- [Como funciona](#how-it-works)
- [Início rápido](#quick-start)
- [Interface web (lab-builder)](#web-ui-lab-builder)
- [Formato de definição do laboratório](#lab-definition-format)
- [Exemplos](#examples)
- [Guias passo a passo](#step-by-step-walkthroughs)
- [Comandos disponíveis](#available-commands)
- [Add-ons disponíveis](#available-addons)
- [Referência de configuração](#configuration-reference)
- [Testes](#testing)
- [Contribuindo / Configuração de desenvolvimento](#contributing--developer-setup)

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="architecture"></a>
## Arquitetura

<p align="center" float="left">
  <kbd><picture><source media="(prefers-color-scheme: dark)" srcset="media/diagram1-dark.svg"><img src="media/diagram1.svg" width="800" alt="Diagrama geral da arquitetura"/></picture></kbd>
  <kbd><picture><source media="(prefers-color-scheme: dark)" srcset="media/diagram2-dark.svg"><img src="media/diagram2.svg" width="800" alt="Diagrama de rede e serviços"/></picture></kbd>
</p>

O sistema é construído em torno de uma **arquitetura de dois níveis**:

```mermaid
graph TB
    Operator["Cliente do operador"] -->|"SSH / DNS / HTTP"| AutoVM
    subgraph HV["Nó(s) hipervisor — KVM/QEMU"]
        AutoVM["VM de automação<br/>DNS · HTTP · scripts · interface web"]
        AutoVM -->|"virt-install / virsh"| VM1["VM do laboratório"]
        AutoVM -->|"virt-install / virsh"| VM2["VM do laboratório"]
        AutoVM -->|"virt-install / virsh"| VM3["VM do laboratório"]
    end
```

### Nó(s) hipervisor

Uma ou mais máquinas físicas (bare-metal) executando KVM/QEMU. Cada uma hospeda as VMs do laboratório e guarda as imagens QCOW2 de origem em `/var/lib/libvirt/images/sources/`. Um NUC, uma workstation, ou qualquer máquina x86_64 capaz de rodar KVM serve. Laboratórios que precisam de mais capacidade do que uma única máquina podem se espalhar por **múltiplos hosts KVM** — veja [laboratórios multi-host](#multi-host-labs) abaixo.

### VM de automação

Uma VM pequena rodando no hipervisor que atua como o plano de controle de todo o laboratório. Ela fornece:

- **DNS** — o BIND (`named`) serve o domínio do laboratório e encaminha requisições externas, de modo que todos os hostnames do laboratório se resolvem a partir de qualquer cliente que aponte para ela
- **HTTP** — serve os arquivos de provisionamento (Ignition, Combustion, cloud-init) em `/srv/www/htdocs/lab_creation/`
- **Scripts** — todos os comandos de gerenciamento do laboratório, instalados em `/usr/local/bin/`
- **Interface web** (opcional) — [lab-builder](#web-ui-lab-builder), um designer de lab.json baseado em navegador

Todos os comandos do usuário são executados **na VM de automação**. Ela se conecta ao(s) hipervisor(es) e às VMs criadas via SSH. Nenhum acesso direto ao hipervisor é necessário após a configuração inicial.

### Por baixo dos panos

As ferramentas de linha de comando e cada add-on são em Python 3.11, vivem em `libs/` e `scripts/` e são instalados em `/usr/local/lib/lab_creation/` — organizados em torno de um pequeno conjunto de módulos de biblioteca compartilhados (`lab_creation.py`, `backends.py`, `services.py`, `spacecmd_common.py`, …), em vez de dependerem uns dos outros. A criação de VMs passa por uma interface `VMBackend` plugável (`LibvirtBackend` hoje), de modo que o mesmo código de orquestração possa eventualmente atingir outros backends de virtualização (KubeVirt, Harvester) sem tocar nos add-ons. Um add-on legado (`install_ds389`) ainda é bash puro — é anterior à migração para Python e já estava quebrado em bash, então não valeu a pena portá-lo. A implementação da era bash que estes substituíram continua viva, arquivada, em `legacy_bash/`.

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="how-it-works"></a>
## Como funciona

### Pipeline de implantação

O `setup_lab.py` executa uma sequência fixa de fases; as duas fases exclusivas do Kubernetes são completamente ignoradas em um laboratório somente de VMs (sem a seção `kclusters`):

```mermaid
flowchart LR
    A["phase_services"] -->|"com kclusters"| C["phase_dns"]
    A -->|"sem kclusters"| D["phase_create_vms"]
    C --> D["phase_create_vms"]
    D -->|"com kclusters"| F["phase_reboot_and_wait_kept_nodes"]
    D -->|"sem kclusters"| H["phase_vm_addons"]
    F --> G["phase_install_k8s_and_addons"]
    G --> H["phase_vm_addons"]
```

### Provisionamento de VMs

Cada VM é criada assim:
1. Resolvendo a qual host KVM ela pertence (o campo explícito `kvm_host`, ou autosseleção por capacidade livre — veja [laboratórios multi-host](#multi-host-labs))
2. Copiando e redimensionando uma imagem QCOW2 de origem naquele host
3. Gerando os arquivos de provisionamento a partir de templates, conforme `config_method`
4. Registrando uma entrada DNS no BIND
5. Executando `virt-install` no hipervisor via SSH
6. Aguardando o SSH ficar disponível

O método de provisionamento é controlado por `config_method` no JSON do laboratório (por nó ou em `common`):

| Valor | Método | Usado para |
|---|---|---|
| _(vazio, padrão)_ | Ignition + Combustion | SLE Micro |
| `cloud-init` | ISO de cloud-init | openSUSE Leap, Ubuntu |
| `virt_customize` | Modifica a QCOW2 diretamente no hipervisor (`virt-customize`) — não requer suporte a Ignition/cloud-init no convidado | CentOS 7, Debian/RHEL antigos, ou qualquer imagem sem Ignition/cloud-init |
| `install_iso` | Instalação com script a partir de uma ISO de instalador real (AutoYaST, Kickstart, Preseed ou AutoInstall, conforme `install_type`) | Distribuições sem nenhuma outra via de provisionamento |

### Backends de VM

Qual tecnologia de hipervisor realmente cria um nó é decidido por uma interface `VMBackend` intercambiável, resolvida uma vez por nó (`backend: harvester` na configuração daquele nó seleciona o `HarvesterBackend`; qualquer outro valor usa o `LibvirtBackend` por padrão) — todo add-on e script de orquestração conversa com o backend resolvido da mesma forma, seja ele qual for:

```mermaid
graph TD
    SV["setup_vm.py / setup_lab.py"] --> GB["backends.get_backend()"]
    GB -- "padrão" --> LB["LibvirtBackend"]
    GB -- "backend: harvester" --> HB["HarvesterBackend"]
    LB --> KVM["virt-install / virsh<br/>em um hipervisor KVM"]
    HB --> KV["KubeVirt VirtualMachine<br/>em um cluster Harvester"]
```

### Configuração do Kubernetes

Depois que as VMs estão de pé, o `setup_lab.py` instala o Kubernetes em cada nó conforme a seção `kclusters` do JSON. Tanto RKE2 quanto K3s são suportados. Assim que um cluster está pronto, seus add-ons rodam em sequência; add-ons de nível de VM (associados a um único nó em vez de a um cluster) rodam depois que aquele nó é provisionado.

### Laboratórios multi-host

<a id="multi-host-labs"></a>
Um laboratório não fica limitado a um único hipervisor. Defina `KVM_HOSTS` (separados por espaço) em `/etc/lab_creation.cfg` na VM de automação para disponibilizar mais de um hipervisor:

```ini
KVM_HOSTS="hv1.mydemo.lab hv2.mydemo.lab hv3.mydemo.lab"
```

Depois, para cada nó no JSON do laboratório, você pode:
- **fixá-lo explicitamente** — `"kvm_host": "hv2.mydemo.lab"` na configuração daquele nó, ou
- **deixar que se autosselecione** — omita `kvm_host`; o nó cai no host configurado que tiver, no momento, CPU/RAM/disco livres suficientes (verificado ao vivo via SSH).

Nós que não especificam `kvm_host` e máquinas com apenas um host configurado se comportam exatamente como antes de essa funcionalidade existir — nada muda para um laboratório de hipervisor único.

### Ordem de carregamento das bibliotecas

Cada script carrega sua configuração nesta ordem:

1. `/etc/lab_creation.defaults` — padrões de todo o sistema, caminhos, listas de pacotes
2. `/usr/local/lib/lab_creation/primary.py` — validação de entrada, carregamento de configuração
3. `/etc/lab_creation.cfg` — configurações específicas do nó (`REMOTE_HOST`, `ROOT_SSH_KEY`, `VIRT_SRV`, `KVM_HOSTS`, etc.)
4. `/usr/local/lib/lab_creation/lab_creation.py` — funções de VM, DNS e orquestração
5. `/usr/local/lib/lab_creation/k8s.py` — funções de cluster Kubernetes

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="quick-start"></a>
## Início rápido

```mermaid
flowchart TD
    S1["1. Preparar o sistema operacional do hipervisor"] --> S2["2. Preparar os scripts de instalação"]
    S2 --> S3["3. Configurar e executar a configuração do nó KVM"]
    S3 --> S4["4. Configurar a VM de automação"]
    S4 --> S5["5. Apontar o DNS do cliente para a VM de automação"]
    S5 --> S6["6. Construir seu primeiro laboratório"]
```

### Requisitos

- Uma máquina capaz de rodar KVM (Intel VT-x ou AMD-V habilitado)
- Acesso à internet (ou um mirror local) para download de pacotes e imagens
- Uma imagem QCOW2 do sistema operacional escolhido, colocada em `/var/lib/libvirt/images/sources/` no hipervisor

> [!IMPORTANT]
> A VM de automação precisa especificamente do `python3.11` — o toolchain é fixado explicitamente nele. A maioria das distribuições traz junto um `python3` padrão mais antigo; o script de instalação se recusa a continuar se o `python3.11` estiver ausente.

Imagens testadas:
- [SLE Micro](https://www.suse.com/download/sle-micro/) — recomendado, usado com Ignition+Combustion
- openSUSE Leap Micro — suportado, usado com cloud-init

### Passo 1 — Preparar o sistema operacional do hipervisor

Instale o SLES (ou outro Linux capaz de rodar KVM) no seu hardware. Durante a instalação, escolha:
- **Rede**: crie uma interface bridge (`br0`) ligada à sua NIC principal com IP estático
- **Papel do sistema**: KVM Virtualization Host

<details>
<summary>Gravando um USB inicializável a partir do Linux</summary>

```shell
# Antes de inserir o USB:
cat /proc/partitions > /tmp/partb4

# Insira o USB, então:
cat /proc/partitions > /tmp/parta

# Encontre o novo dispositivo:
diff /tmp/part*
```

> [!WARNING]
> O próximo comando **destrói todos os dados** do dispositivo alvo. Confira novamente o `sdX` contra a saída do `diff` acima antes de executá-lo.

```shell
# Grave a ISO (substitua sdX pelo seu dispositivo):
dd if=SLE-15-SP6-Online-x86_64-GM-Media1.iso of=/dev/sdX bs=4k && sync
```

</details>

### Passo 2 — Preparar os scripts de instalação

De qualquer máquina Linux com acesso SSH ao hipervisor:

```shell
curl https://raw.githubusercontent.com/SUSE-Technical-Marketing/lab-in-a-box/main/install_demo_server_scripts.sh | bash -
```

Isso baixa os scripts de configuração para `/var/tmp/setup_demo_server/`.

### Passo 3 — Configurar e executar a configuração do nó KVM

<a id="step-3--configure-and-run-the-kvm-node-setup"></a>

```shell
cd /var/tmp/setup_demo_server/setup_demo_server/
vim lab.cfg
```

Configurações-chave em `lab.cfg`:

| Configuração | Descrição |
|---|---|
| `ROOT_PWD_HASH` | Hash da senha de root — gerado com `mkpasswd --method=SHA-512 --stdin` |
| `ROOT_SSH_PUB_KEY` | Sua chave pública SSH para acesso sem senha |
| `AUTOMATION_HOSTNAME` | Hostname da VM de automação (ex.: `automation.mydemo.lab`) |
| `_QCOW_IMAGE` | Nome do arquivo da imagem QCOW2 de origem |
| Configurações de rede | IP, gateway, máscara, DNS para a rede do laboratório |

Depois execute a configuração (substitua `<IP>` pela IP do seu hipervisor, ou omita para local):

```shell
./setup_kvm_node.py <IP>
```

Isso provisiona a VM de automação e inicia todos os serviços necessários.

### Passo 4 — Configurar a VM de automação

Conecte-se via SSH à VM de automação e instale os scripts do laboratório:

```shell
ssh <AUTOMATION_HOSTNAME>
./install_automation_node_scripts.sh

cp /etc/lab_creation.cfg.example /etc/lab_creation.cfg
vim /etc/lab_creation.cfg
```

Configurações-chave em `lab_creation.cfg`:

| Configuração | Descrição |
|---|---|
| `REMOTE_HOST` | Hostname ou IP do hipervisor KVM (primário) |
| `KVM_HOSTS` | _(opcional)_ lista de hipervisores adicionais separados por espaço para um [laboratório multi-host](#multi-host-labs) |
| `ROOT_SSH_KEY` | Conteúdo da chave pública SSH a ser injetada nas VMs |
| `VIRT_SRV` | URI de conexão do libvirt (ex.: `qemu+ssh://root@hypervisor/system`) |
| `NETWORK` | Rede libvirt padrão para as VMs (ex.: `bridge=br0`) |

### Passo 5 — Apontar o DNS do seu cliente para a VM de automação

<a id="step-5--point-your-client-dns-at-the-automation-vm"></a>

Para que os hostnames se resolvam a partir do seu desktop:

```shell
# Linux (NetworkManager):
nmcli con mod <connection> ipv4.dns <AUTOMATION_IP>

# Ou adicione a /etc/resolv.conf:
nameserver <AUTOMATION_IP>
```

### Passo 6 — Construa seu primeiro laboratório

```shell
setup_lab.py examples/cluster.json.template
```

Veja [Exemplos](#examples) abaixo para mais pontos de partida, ou abra a [interface web](#web-ui-lab-builder) em vez de escrever JSON à mão.

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="web-ui-lab-builder"></a>
## Interface web (lab-builder)

Um designer de arquivos `lab.json` baseado em navegador que **introspecciona as próprias bibliotecas Python do projeto em tempo de execução** — não tem nenhum conhecimento fixo de nenhum add-on. Escolha um componente e ele gera um formulário diretamente a partir do esquema daquele componente; adicione um campo a um script, e a interface o exibe sem nenhuma mudança no frontend.

```shell
# A forma mais rápida de experimentar — sem dependências além do Python:
python3.11 webui/run-local.py            # → http://localhost:8677/
```

Para um deployment de produção (Apache, ou um serviço standalone systemd/independente de init, mais HTTPS via um certificado autoassinado gerado de forma idempotente), veja **[README.webui.md](README.webui.md)** — que cobre os três modos de deployment, a API HTTP e a resolução de problemas.

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="lab-definition-format"></a>
## Formato de definição do laboratório

Laboratórios são definidos como arquivos JSON ou YAML (detectado automaticamente — veja a nota abaixo). O formato atual suporta múltiplos clusters Kubernetes por laboratório (`kclusters`); veja `examples/cluster.json.template` para o formato legado de cluster único (`cluster`).

```mermaid
graph TD
    Lab["lab.json"] --> Nodes["nodes<br/>por VM: myip, mymac, kcluster, addons..."]
    Lab --> Common["common<br/>padrões compartilhados: ISO_IMAGE, VM_MEM, VM_DSK..."]
    Lab --> KClusters["kclusters<br/>clu_type, clu_rel, mydomain, addons"]
    Lab --> AddonSections["uma seção por add-on<br/>ex.: rancher, longhorn"]
    Nodes -. "kcluster" .-> KClusters
    KClusters -. "addons" .-> AddonSections
    Nodes -. "addons" .-> AddonSections
```

```jsonc
{
  "nodes": {
    "node101.mydemo.lab": {
      "myip":  "192.168.88.101",
      "mymac": "34:8a:b1:4b:1a:c1",
      "INSTALL_RKE2_TYPE": "server",   // "server" ou "agent"
      "kcluster": "cluster1"           // a qual entrada de kclusters este nó pertence
    },
    "node102.mydemo.lab": {
      "myip":  "192.168.88.102",
      "mymac": "34:8a:b1:4b:1a:c2",
      "INSTALL_RKE2_TYPE": "agent",
      "kcluster": "cluster1"
    }
  },
  "common": {
    "ISO_IMAGE":  "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2",
    "VM_MEM":     "24576",
    "VM_DSK":     "80",
    "VM_CPU":     "6",
    "VM_BOOT":    "uefi",             // uefi (padrão), firmware=bios, bios, uefi=off
    "mymask":     "24",
    "mygw":       "192.168.88.1",
    "mydns":      "192.168.88.73",
    "mynet_reverse": "88.168.192"
  },
  "kclusters": {
    "cluster1": {
      "clu_type":  "rke2",             // "rke2" ou "k3s"
      "clu_rel":   "stable",
      "mydomain":  "mydemo.lab",
      "addons": ["rancher", "longhorn"]
    }
  },
  "rancher": {
    "rancher_shorthn":   "rancher",
    "rancher_rel":       "rancher-prime",
    "rancher_repo_url":  "https://charts.rancher.com/server-charts/prime",
    "rancher_helm_rel":  "rancher",
    "rancher_helm_chart": "rancher-prime/rancher",
    "rancher_Version":   "--version 2.13.3",
    "cert_manager_ver":  "--version v1.14.4"
  }
}
```

<details>
<summary>O mesmo laboratório, em YAML</summary>

```yaml
nodes:
  node101.mydemo.lab:
    myip: "192.168.88.101"
    mymac: "34:8a:b1:4b:1a:c1"
    INSTALL_RKE2_TYPE: server   # "server" ou "agent"
    kcluster: cluster1          # a qual entrada de kclusters este nó pertence
  node102.mydemo.lab:
    myip: "192.168.88.102"
    mymac: "34:8a:b1:4b:1a:c2"
    INSTALL_RKE2_TYPE: agent
    kcluster: cluster1

common:
  ISO_IMAGE: SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2
  VM_MEM: "24576"
  VM_DSK: "80"
  VM_CPU: "6"
  VM_BOOT: uefi                # uefi (padrão), firmware=bios, bios, uefi=off
  mymask: "24"
  mygw: "192.168.88.1"
  mydns: "192.168.88.73"
  mynet_reverse: "88.168.192"

kclusters:
  cluster1:
    clu_type: rke2              # "rke2" ou "k3s"
    clu_rel: stable
    mydomain: mydemo.lab
    addons: [rancher, longhorn]

rancher:
  rancher_shorthn: rancher
  rancher_rel: rancher-prime
  rancher_repo_url: https://charts.rancher.com/server-charts/prime
  rancher_helm_rel: rancher
  rancher_helm_chart: rancher-prime/rancher
  rancher_Version: "--version 2.13.3"
  cert_manager_ver: "--version v1.14.4"
```

</details>

Campos opcionais no nível do nó:

| Campo | Descrição |
|---|---|
| `addons` | Lista de scripts de add-on a executar apenas para esta VM |
| `config_method` | Sobrescreve o método de provisionamento (`cloud-init`, `virt_customize`, `install_iso`) |
| `kvm_host` | Fixa esta VM a um hipervisor específico em um [laboratório multi-host](#multi-host-labs) |
| `extra_dsk` | Disco(s) adicional(is) a conectar — `"/dev/sdb"`, ou `"/dev/sdb,bus=scsi"` para sobrescrever o barramento padrão por disco |
| `salt_states` | Estados Salt a aplicar (apenas com o método cloud-init) |

Campos opcionais de kcluster:

| Campo | Descrição |
|---|---|
| `mgm_node` | Hostname do nó que executa os instaladores de add-ons do cluster; por padrão o primeiro nó servidor |

Cada script de add-on também aceita `--schema` (alias de `--input-definition`), que imprime suas próprias chaves de configuração em JSON ou YAML legível por máquina — o mesmo esquema que a [interface web](#web-ui-lab-builder) lê para construir seus formulários:

```shell
install_longhorn --schema
setup_lab.py --input-definition yaml   # esquema base de topologia (common/nodes/kclusters)
```

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="examples"></a>
## Exemplos

### Laboratório mínimo de uma única VM

O menor laboratório possível — uma VM, sem Kubernetes:

```jsonc
{
  "nodes": {
    "standalone.mydemo.lab": { "myip": "192.168.88.50" }
  },
  "common": {
    "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2",
    "VM_MEM": "4096", "VM_DSK": "40", "VM_CPU": "2",
    "mymask": "24", "mygw": "192.168.88.1", "mydns": "192.168.88.73"
  }
}
```

```shell
setup_lab.py standalone.json
```

### RKE2 + Rancher + Longhorn (o cluster "hello world")

Um cluster de 2 nós com uma plataforma de gestão e armazenamento distribuído — veja o exemplo completo em [Formato de definição do laboratório](#lab-definition-format) acima.

```shell
setup_lab.py rancher-cluster.json
# Execute novamente mais tarde, pulando qualquer VM que já esteja ativa e acessível:
setup_lab.py --keep rancher-cluster.json
```

### Distribuindo um cluster entre dois hosts

Fixe o servidor em um hipervisor e deixe os agentes se autoposicionarem em qualquer um dos [hosts configurados](#multi-host-labs) que tenha espaço:

```jsonc
{
  "nodes": {
    "srv1.mydemo.lab":   { "myip": "192.168.88.10", "kvm_host": "hv1.mydemo.lab", "kcluster": "prod" },
    "agent1.mydemo.lab": { "myip": "192.168.88.11", "kcluster": "prod" },
    "agent2.mydemo.lab": { "myip": "192.168.88.12", "kcluster": "prod" }
  },
  "common": { "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2", "VM_MEM": "8192", "VM_DSK": "60", "VM_CPU": "4" },
  "kclusters": { "prod": { "clu_type": "rke2", "clu_rel": "stable", "mydomain": "mydemo.lab" } }
}
```

### Servidor SUSE Multi-Linux Manager (Uyuni) + um cliente registrado

Suba um servidor Uyuni com uma chave de ativação, depois registre uma segunda VM como cliente Salt contra ele — veja [Add-ons disponíveis](#available-addons) para o conjunto completo de recursos (`orgs`, RBAC, Content Lifecycle Management, integração com Ansible, e mais):

```jsonc
{
  "nodes": {
    "uyuni.mydemo.lab":  { "myip": "192.168.88.30", "addons": ["uyuni"] },
    "client1.mydemo.lab": { "myip": "192.168.88.31", "addons": ["client_registration"] }
  },
  "common": { "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2", "VM_MEM": "8192", "VM_DSK": "60", "VM_CPU": "4" },
  "uyuni": {
    "uyuni_admin": "admin", "uyuni_password": "Uyuni12345",
    "uyuni_activation_key": "1-lab-clients", "uyuni_activation_key_base_channel": "sle-micro-6.1-pool"
  },
  "client_registration": {
    "client_registration_server_type": "uyuni",
    "client_registration_server": "uyuni.mydemo.lab",
    "client_registration_activation_key": "1-lab-clients"
  }
}
```

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="step-by-step-walkthroughs"></a>
## Guias passo a passo

Os [Exemplos](#examples) acima são pontos de partida para copiar e colar. Estes três guias percorrem cenários completos e reais do início ao fim — o que executar, o que acontece em cada etapa e como verificar que realmente funcionou. Cada campo JSON e formato de comando abaixo corresponde à própria suíte de testes deste projeto (`tests/run_tests.sh`) e ao código-fonte.

> [!TIP]
> Os guias 2 e 3 são **testados ao vivo** — executados contra um servidor/hardware real, não apenas verificados isoladamente.

### Guia 1 — Seu primeiro cluster: RKE2 + Rancher + Longhorn

Objetivo: duas VMs SLE Micro, um cluster RKE2, Rancher para gestão, Longhorn para armazenamento — acessível pelo navegador ao final.

1. **Escreva o arquivo do laboratório.** Salve como `rancher-cluster.json` (ajuste IPs/rede para o seu domínio de laboratório):

   ```jsonc
   {
     "nodes": {
       "node101.mydemo.lab": {
         "myip": "192.168.88.101", "mymac": "34:8a:b1:4b:1a:c1",
         "INSTALL_RKE2_TYPE": "server", "kcluster": "cluster1"
       },
       "node102.mydemo.lab": {
         "myip": "192.168.88.102", "mymac": "34:8a:b1:4b:1a:c2",
         "INSTALL_RKE2_TYPE": "agent", "kcluster": "cluster1"
       }
     },
     "common": {
       "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2",
       "VM_MEM": "24576", "VM_DSK": "80", "VM_CPU": "6",
       "mymask": "24", "mygw": "192.168.88.1", "mydns": "192.168.88.73"
     },
     "kclusters": {
       "cluster1": {
         "clu_type": "rke2", "clu_rel": "stable", "mydomain": "mydemo.lab",
         "addons": ["rancher", "longhorn"]
       }
     },
     "rancher": {
       "rancher_shorthn": "rancher", "rancher_rel": "rancher-prime",
       "rancher_repo_url": "https://charts.rancher.com/server-charts/prime",
       "rancher_helm_rel": "rancher", "rancher_helm_chart": "rancher-prime/rancher",
       "rancher_Version": "--version 2.13.3", "cert_manager_ver": "--version v1.14.4"
     }
   }
   ```

2. **Construa:**

   ```shell
   setup_lab.py rancher-cluster.json
   ```

   O `setup_lab.py` faz o preflight do arquivo automaticamente antes de fazer qualquer outra coisa — IPs errados, uma referência a `kcluster` que não existe, um `ISO_IMAGE` ausente e erros parecidos são detectados e impressos (`✗ Preflight FAILED — N error(s)`) sem nada ser criado, em vez de falhar no meio do caminho. Um arquivo correto imprime `✓ Preflight passed` e segue direto para a construção.

   Em ordem, isto: registra ambos os nós no DNS → cria ambas as VMs (copia a imagem QCOW2, gera os arquivos de Combustion, as inicializa, aguarda o SSH) → instala o RKE2 em `node101` como servidor, depois em `node102` como agente → instala `rancher` e `longhorn` no nó de gestão do cluster (`mgm_node`, por padrão o primeiro nó servidor — aqui `node101`). Um cluster de 2 nós com Rancher normalmente leva de 15 a 25 minutos; a maior parte é o bootstrap do RKE2 e a própria instalação Helm do Rancher.

3. **Verifique se o DNS resolve** (do seu próprio desktop, uma vez [apontado para o DNS da VM de automação](#step-5--point-your-client-dns-at-the-automation-vm)):

   ```shell
   dig +short node101.mydemo.lab rancher.mydemo.lab
   ```

   Ambos devem retornar `192.168.88.101` (o hostname de ingress do Rancher é o valor de `rancher_shorthn`, `rancher`, sob o `mydomain` do cluster).

4. **Faça login.** Acesse `https://rancher.mydemo.lab` (certificado autoassinado — seu navegador avisará uma vez) e faça login com `rancher_initial_pwd` de `/etc/lab_creation.cfg` na VM de automação.

5. **Itere sem reconstruir tudo.** Mudou a configuração de um nó, ou uma VM travou? Execute novamente com `--keep`: qualquer VM que já exista, corresponda à sua IP/MAC definida e esteja acessível via SSH é deixada intocada; apenas o que realmente está faltando ou quebrado é (re)criado:

   ```shell
   setup_lab.py --keep rancher-cluster.json
   ```

6. **Destrua** quando terminar:

   ```shell
   destroy_lab.py rancher-cluster.json
   ```

### Guia 2 — Servidor SUSE Multi-Linux Manager (Uyuni) com um cliente registrado

Objetivo: um servidor Uyuni com uma chave de ativação real, e uma segunda VM que se registra como cliente gerenciado por Salt contra ele. **Testado ao vivo** de ponta a ponta contra um servidor Uyuni real.

1. **Escreva o arquivo do laboratório** — um nó para o servidor Uyuni, outro para o cliente:

   ```jsonc
   {
     "nodes": {
       "uyuni.mydemo.lab":   { "myip": "192.168.88.30", "addons": ["uyuni"] },
       "client1.mydemo.lab": { "myip": "192.168.88.31", "addons": ["client_registration"] }
     },
     "common": {
       "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2",
       "VM_MEM": "8192", "VM_DSK": "60", "VM_CPU": "4",
       "mymask": "24", "mygw": "192.168.88.1", "mydns": "192.168.88.73", "mydomain": "mydemo.lab"
     },
     "uyuni": {
       "uyuni_admin": "admin", "uyuni_password": "Uyuni12345",
       "uyuni_activation_key": "1-lab-clients", "uyuni_activation_key_base_channel": "sle-micro-6.1-pool"
     },
     "client_registration": {
       "client_registration_server_type": "uyuni",
       "client_registration_server": "uyuni.mydemo.lab",
       "client_registration_activation_key": "1-lab-clients"
     }
   }
   ```

2. **Construa:**

   ```shell
   setup_lab.py uyuni-lab.json
   ```

   Add-ons de nível de VM (tanto `uyuni` quanto `client_registration` são associados ao nó, não ao cluster, já que não há seção `kclusters` aqui) rodam assim que seu próprio nó fica de pé. O `install_uyuni` sobe o servidor, aguarda ele ficar acessível, e então cria a chave de ativação. O `install_client_registration` então faz o bootstrap do `client1` contra ele — instala o script de bootstrap, o executa, e verifica periodicamente até que a chave Salt do novo minion apareça como pendente, e então a aceita.

3. **Verifique se o cliente realmente se registrou.** Conecte via SSH ao servidor Uyuni e pergunte diretamente a ele:

   ```shell
   ssh uyuni.mydemo.lab
   mgrctl exec 'spacecmd -- system_list'
   ```

   `client1.mydemo.lab` deve aparecer na lista.

4. **Faça login na interface web** em `https://uyuni.mydemo.lab` com `uyuni_admin`/`uyuni_password` para ver a mesma coisa visualmente, navegar pela chave de ativação, ou rodar um highstate.

Ponto conhecido do projeto upstream (não é um bug deste projeto, documentado aqui caso você encontre): o próprio scriptlet de atualização de pacotes do `salt-transactional-update` pode deixar uma chave YAML duplicada em `/etc/salt/minion.d/transactional_update.conf` no cliente, fazendo o `salt-minion` entrar em loop de falhas até que isso seja deduplicado manualmente. Nada neste repositório mexe nesse arquivo.

### Guia 3 — VM de automação sob NAT (notebook com uma única NIC como hipervisor)

Objetivo: fazer o bootstrap da VM de automação em um host sem NIC sobrando para fazer bridge — em vez disso, uma rede privada gerenciada pelo libvirt, com portas específicas redirecionadas via DNAT a partir da própria IP real do host. **Testado ao vivo** de ponta a ponta em uma VM aninhada descartável.

Isso não muda nada no fluxo padrão do [Início rápido](#quick-start) se você não optar por ele — `_network_mode` é `"bridge"` por padrão, byte a byte igual a qualquer configuração já existente.

1. **No `lab.cfg`** ([Passo 3](#step-3--configure-and-run-the-kvm-node-setup) do Início rápido), defina:

   ```ini
   _network_mode="nat"
   _nat_network_name="labnat"          # valor padrão mostrado — uma nova rede virtual do libvirt, não a LAN real do seu host
   _nat_network_cidr="192.168.150.0/24" # valor padrão mostrado
   _nat_forwarded_ports="22:22/TCP 80:80/TCP 443:443/TCP"  # valor padrão mostrado — "<porta no IP REAL do HIPERVISOR>:<porta na VM de automação>/<protocolo>"
   ```

   Isso encaminha as portas 22/80/443 no **IP real e acessível externamente do hipervisor** (`<externa>`) para as mesmas portas no **endereço NAT privado da VM de automação** (`<interna>`) — neste ponto, a VM de automação é a única coisa ouvindo nessa rede privada, então "interna" sempre significa "na VM de automação" aqui. (O passo 5, mais abaixo, reutiliza exatamente essa mesma sintaxe `<externa>:<interna>/<protocolo>` para encaminhar para uma VM do *lab* em vez disso, assim que uma existir — ali, "interna" passa a significar o endereço NAT privado dessa VM, não o da VM de automação.)

2. **Execute a configuração exatamente como sempre:**

   ```shell
   ./setup_kvm_node.py
   ```

   Isso define a rede libvirt `labnat` (com NAT, DHCP/gateway tratados pelo próprio libvirt — o mesmo mecanismo da rede `default` embutida do libvirt, só que com o nome/CIDR próprios deste projeto) em vez de uma bridge, depois cria a VM de automação nela com um IP estático dentro dessa faixa privada, e então redireciona (DNAT) as três portas acima a partir da própria IP real do host.

3. **Verifique se a rede e as regras de encaminhamento existem**, no hipervisor:

   ```shell
   virsh net-list                              # labnat: active
   iptables -t nat -L LAB_PORTFWD -n -v         # regras DNAT para a IP privada da VM de automação
   iptables -L LAB_PORTFWD_FWD -n -v            # regras ACCEPT correspondentes na cadeia FORWARD
   ```

4. **Acesse a VM de automação de fora do hipervisor**, usando a própria IP real do hipervisor — não o endereço privado `192.168.150.x` da VM de automação, que não é roteável de nenhum outro lugar:

   ```shell
   ssh root@<ip-real-do-hipervisor>          # redirecionado (DNAT) para o SSH da VM de automação, porta 22
   ```

   As portas 80 e 443 também são encaminhadas por padrão (o servidor HTTP de arquivos de provisionamento e, assim que você configurar a [interface web](#web-ui-lab-builder), seu listener HTTPS) — acessíveis da mesma forma, através da IP real do hipervisor.

5. **Adicione encaminhamento para uma VM do laboratório**, não apenas para a própria VM de automação: dê a esse nó um campo `forwarded_ports` e ative o serviço `portforward` uma vez, em `common.services`:

   ```jsonc
   {
     "nodes": {
       "app1.mydemo.lab": {
         "myip": "192.168.150.20",
         "forwarded_ports": ["8080:80/TCP", "8443:443/TCP"]
       }
     },
     "common": { "services": ["portforward"], "...": "…restante de common como de costume" }
   }
   ```

   `setup_lab.py`/`setup_vm.py` encaminham (DNAT) essas duas portas a partir da IP real do hipervisor da mesma forma, na primeira vez que qualquer nó do laboratório declara `forwarded_ports`.

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="available-commands"></a>
## Comandos disponíveis

Todos os comandos rodam na **VM de automação** e recebem um arquivo JSON de definição de laboratório como primeiro argumento.

| Comando | Descrição |
|---|---|
| `setup_lab.py [--keep] <lab.json>` | Cria todas as VMs, configura os clusters Kubernetes e instala cada add-on de cluster e de VM em ordem. `--keep` pula qualquer VM que já exista, corresponda à IP/MAC definida e esteja acessível via SSH — sem essa opção, cada VM é destruída e recriada. |
| `setup_vm.py <lab.json> <hostname>` | Cria ou recria uma única VM |
| `destroy_vm.py <lab.json> <hostname>` | Destrói uma única VM |
| `destroy_lab.py <lab.json>` | Destrói todas as VMs de um laboratório |

Cada comando e cada script `install_<addon>` aceita:

```shell
setup_lab.py --version              # imprime a versão instalada
install_longhorn --schema           # imprime o esquema de configuração deste add-on (JSON)
install_longhorn --schema yaml      # ...ou YAML
```

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="available-addons"></a>
## Add-ons disponíveis

Add-ons são referenciados pelo nome no array `addons` de um kcluster ou nó. O script `install_<name>` correspondente precisa estar no `PATH`.

<sub>Ir para: <a href="#addons-k8s">Kubernetes &amp; GitOps</a> · <a href="#addons-security">Segurança &amp; conformidade</a> · <a href="#addons-suma">SUSE Multi-Linux Manager / Uyuni</a> · <a href="#addons-storage">Armazenamento &amp; bancos de dados</a> · <a href="#addons-collaboration">Colaboração &amp; compartilhamento de arquivos</a> · <a href="#addons-cicd">CI/CD &amp; ferramentas</a> · <a href="#addons-ai">IA / ML</a> · <a href="#addons-virt">Virtualização</a> · <a href="#addons-demos">Aplicações de demonstração</a> · <a href="#addons-games">Jogos</a></sub>

<a id="addons-k8s"></a>
<details open>
<summary><strong>Plataforma Kubernetes &amp; GitOps</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`rancher`](https://www.rancher.com/) | Plataforma de gestão de Kubernetes SUSE Rancher Prime |
| [`longhorn`](https://longhorn.io/) | Armazenamento em bloco distribuído SUSE Longhorn |
| [`harbor`](https://goharbor.io/) | Registro de containers |
| [`argocd`](https://argo-cd.readthedocs.io/) | Controlador GitOps Argo CD |
| [`kubewarden`](https://www.kubewarden.io/) | Motor de políticas do Kubernetes |
| [`istio`](https://istio.io/) | Service mesh |
| [`linkerd`](https://linkerd.io/) | Service mesh |
| [`traefik`](https://traefik.io/) | Controlador de ingress |
| [`nginx`](https://nginx.org/) | Controlador de ingress / proxy reverso |
| [`coredns`](https://coredns.io/) | DNS do cluster |
| [`kucero`](https://github.com/SUSE/kucero) | Rotação de certificados do cluster Kubernetes |
| [`fluid`](https://fluid-cloudnative.github.io/) | Orquestração/cache de dados para cargas de trabalho cloud-native |
| [`suse_observability`](https://www.suse.com/products/suse-observability/) | SUSE Observability (métricas/traces/topologia baseadas em StackState) |
| [`agones`](https://agones.dev/) | Hospedagem/escalonamento de servidores de jogo dedicados (agones.dev) |
</details>

<a id="addons-security"></a>
<details open>
<summary><strong>Segurança &amp; conformidade</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`neuvector`](https://open-docs.neuvector.com/) | Plataforma de segurança de containers SUSE NeuVector |
| [`nv_testing`](https://open-docs.neuvector.com/testing/testing) | Cargas de trabalho de teste de segurança do NeuVector (pods nginx/node/redis) |
| [`nv-demo-helm`](https://open-docs.neuvector.com/) | Demonstrações do NeuVector baseadas em Helm |
| [`complianceascode`](https://complianceascode.github.io/) | Operador OpenSCAP/ComplianceAsCode |
| [`keycloak`](https://www.keycloak.org/) | Gestão de identidade e acesso |
| [`kagent`](https://kagent.dev/) | Assistente de segurança de IA agêntica para Kubernetes |
| `insecure_app` | Aplicação web intencionalmente vulnerável (demo/treinamento) |
| [`struts_demo`](https://struts.apache.org/) | Aplicação de demonstração vulnerável Apache Struts2 (CVE-2017-5638) |
</details>

<a id="addons-suma"></a>
<details open>
<summary><strong>SUSE Multi-Linux Manager / Uyuni</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`uyuni`](https://www.uyuni-project.org/) | Servidor Uyuni (upstream): chaves de ativação, organizações, RBAC, Content Lifecycle Management, integração com Ansible, auditoria SCAP/CVE, topologia de ambientes dev/QA/prod — veja `install_uyuni --schema` para a lista completa de campos |
| [`smlm`](https://www.suse.com/products/multi-linux-manager/) | Servidor SUSE Multi-Linux Manager — o mesmo conjunto de recursos que `uyuni`, implantado via Kubernetes/Helm |
| [`smlm_proxy`](https://www.suse.com/products/multi-linux-manager/) | Proxy do SMLM |
| `client_registration` | Registra qualquer VM como cliente Salt de um servidor `uyuni`/`smlm` existente (bootstrap por chave de ativação + aceitação da chave salt) |
| [`suma`](https://www.suse.com/products/multi-linux-manager/) | SUSE Multi-Linux Manager (SUMA), instalado diretamente no sistema operacional via `mgradm` — não em Kubernetes |
| [`ansible_control_node`](https://documentation.suse.com/multi-linux-manager/5.1/en/docs/administration/ansible-setup-control-node.html) | Provisiona um nó de controle Ansible real: instala o `ansible-core`, envia playbooks de exemplo + um script de inventário dinâmico que consulta a própria lista de sistemas do `uyuni`/`smlm`, configura acesso SSH ao restante do laboratório. Combina com o próprio campo `*_ansible_control_nodes` do `uyuni`/`smlm`, que habilita a entitlement "Ansible Control Node" no lado do servidor |
</details>

<a id="addons-storage"></a>
<details open>
<summary><strong>Armazenamento &amp; bancos de dados</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`mariadb`](https://mariadb.org/) | Banco de dados MariaDB — Kubernetes (manifesto sem Helm) ou instalado nativamente no SO; o caminho nativo do SO (`libs/db_common.py`) também é reutilizado diretamente por outros add-ons (`nextcloud`, `seafile`) que precisam de um banco de dados complementar no mesmo host |
| [`postgresql`](https://www.postgresql.org/) | Banco de dados PostgreSQL — Kubernetes (Helm) ou instalado nativamente no SO; o caminho nativo do SO (`libs/db_common.py`) também é reutilizado diretamente por outros add-ons (ex.: `nextcloud`) que precisam de um banco de dados complementar no mesmo host |
| [`openldap`](https://www.openldap.org/) | Serviço de diretório OpenLDAP |
| [`ds389`](https://www.port389.org/) | 389 Directory Server (LDAP) |
</details>

<a id="addons-collaboration"></a>
<details open>
<summary><strong>Colaboração &amp; compartilhamento de arquivos</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`nextcloud`](https://nextcloud.com/) | Nextcloud — containers podman independentes (sem Kubernetes, o único modo que suporta instalações de apps em modo air-gapped) por padrão, ou o chart Helm oficial da comunidade em um cluster Kubernetes; apps opcionais de Talk/Groupware/Flow/Assistant e integração de edição de documentos ONLYOFFICE/Euro-Office/Collabora |
| [`seafile`](https://www.seafile.com/) | Seafile — containers podman independentes (sem Kubernetes) por padrão, ou o chart Helm oficial real em um cluster Kubernetes |
</details>

<a id="addons-cicd"></a>
<details open>
<summary><strong>CI/CD &amp; ferramentas de desenvolvimento</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`jenkins`](https://www.jenkins.io/) | Jenkins CI |
| [`gitlab`](https://about.gitlab.com/) | GitLab — container podman Omnibus independente (sem Kubernetes) por padrão, ou o chart Helm oficial real em um cluster Kubernetes |
| [`appcollection`](https://apps.rancher.io/) | SUSE Application Collection |
| [`stackpack`](https://www.stackstate.com/) | Integração de monitoramento StackState |
| [`trento`](https://www.trento-project.io/) | Monitoramento de infraestrutura SAP |
| [`prometheus`](https://prometheus.io/) | Servidor Prometheus independente (container podman, sem Kubernetes) — coleta os próprios exporters embutidos de um servidor `smlm`/`uyuni` (`smlm_monitoring_enabled`) ou qualquer alvo personalizado de `prometheus_scrape_configs` |
| [`grafana`](https://grafana.com/) | Servidor Grafana independente (container podman, sem Kubernetes) — provisiona automaticamente uma fonte de dados Prometheus e, opcionalmente, o dashboard real da comunidade do Grafana Labs para os próprios exporters de um servidor `smlm`/`uyuni` |
</details>

<a id="addons-ai"></a>
<details open>
<summary><strong>IA / ML</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`ollama`](https://ollama.com/) | Runtime local de LLM |
| [`deepseek`](https://www.deepseek.com/) | Modelo DeepSeek, servido via Ollama |
| [`apertus`](https://www.swiss-ai.org/apertus) | Modelo Apertus (Swiss AI Initiative), servido via Ollama |
| [`gemini`](https://ai.google.dev/gemini-api) | Proxy da API do Google Gemini (LiteLLM) |
| [`anthropic`](https://www.anthropic.com/) | Proxy da API do Anthropic Claude (LiteLLM) |
| [`openai`](https://openai.com/) | Proxy da API da OpenAI (LiteLLM) |
| [`kimi`](https://www.moonshot.ai/) | Proxy da API do Moonshot AI Kimi (LiteLLM) |
| [`open_webui`](https://openwebui.com/) | Frontend de chat para Ollama / endpoints compatíveis com OpenAI |
| [`hermes`](https://github.com/NousResearch/hermes-agent) | Hermes Agent — o agente de IA pessoal autoaprimorável da Nous Research (bot de Telegram/Discord/Slack + dashboard web) |
| [`suse_ai`](https://www.suse.com/solutions/artificial-intelligence/) | Stack de IA próprio da SUSE: Ollama + Open WebUI + Milvus |
| [`milvus`](https://milvus.io/) | Banco de dados vetorial Milvus (busca RAG/embeddings) |
| [`qdrant`](https://qdrant.tech/) | Banco de dados vetorial Qdrant (busca RAG/embeddings) |
| [`weaviate`](https://weaviate.io/) | Banco de dados vetorial Weaviate (busca RAG/embeddings) |
| [`gpu_operator`](https://github.com/NVIDIA/gpu-operator) | NVIDIA GPU Operator (agendamento de GPU para cargas de trabalho de IA) |
| [`qwen`](https://qwenlm.github.io/) | Modelo Qwen2.5, servido via Ollama |
| [`mistral`](https://mistral.ai/) | Modelo Mistral, servido via Ollama |
| [`codellama`](https://github.com/meta-llama/codellama) | Modelo Code Llama, servido via Ollama |
| [`starcoder2`](https://github.com/bigcode-project/starcoder2) | Modelo StarCoder2, servido via Ollama |
| [`phoebe`](https://github.com/SUSE/phoebe) | (veja `install_phoebe --schema`) |
</details>

<a id="addons-virt"></a>
<details open>
<summary><strong>Virtualização</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`harvester`](https://harvesterhci.io/) | Provisionamento de nós SUSE Virtualization (Harvester/KubeVirt) |
| [`kiwi`](https://osinside.github.io/kiwi/) | Construtor de appliances KIWI |

</details>

<a id="addons-demos"></a>
<details open>
<summary><strong>Aplicações de demonstração</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`wordpress`](https://wordpress.org/) | Aplicação de demonstração WordPress + MySQL |
| [`fluentd`](https://www.fluentd.org/) | Agregação de logs |
| [`mailman`](https://www.list.org/) | Gestão de listas de e-mail GNU Mailman 3 + interface web |
| [`mediagoblin`](https://www.mediagoblin.org/) | Plataforma federada de publicação de mídia GNU MediaGoblin |
| [`colt`](https://gitlab.com/NalaGinrut/colt) | Colt, um motor de blog baseado em git sobre GNU Artanis |
| [`wikimusic`](https://codeberg.org/jjba23/wikimusic) | WikiMusic, um CMS de conhecimento musical sobre GNU Artanis |
| [`home_assistant`](https://www.home-assistant.io/) | Plataforma de automação residencial Home Assistant |

</details>

<a id="addons-games"></a>
<details open>
<summary><strong>Jogos</strong></summary>

| Nome do add-on | Descrição |
|---|---|
| [`supertux_classic`](https://github.com/Alzter/SuperTux-Classic) | SuperTux Classic, um jogo de plataforma Godot open source auto-hospedado |
| [`skynet_simulator`](https://github.com/edisgreat/skynet-simulator) | Skynet Simulator, um jogo de quebra-cabeça/idle open source auto-hospedado para navegador |
| [`open_saber`](https://github.com/leandrodreamer/BeepSaber) | Open Saber, um jogo de ritmo/corte de blocos open source auto-hospedado |

</details>

Para adicionar um novo add-on: crie `scripts/install_<name>.py` seguindo o padrão de um já existente (importe `addon_common`, carregue a seção JSON correspondente via `load_definition()`, faça o trabalho via SSH), adicione templates em `templates/addons/<name>/` se necessário, e referencie `"<name>"` no array `addons` do seu JSON — o loop de deploy do `install_automation_node_scripts.sh` e a interface web descobrem isso automaticamente.

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="configuration-reference"></a>
## Referência de configuração

### `/etc/lab_creation.defaults`

Padrões de todo o sistema, carregados por cada script. Define caminhos, temporizadores de espera padrão e listas de pacotes. **Não edite** a menos que saiba o que está fazendo.

### `/etc/lab_creation.cfg`

Configuração específica do nó para a VM de automação. Copiado de `/etc/lab_creation.cfg.example` durante a configuração. Variáveis-chave:

| Variável | Descrição |
|---|---|
| `REMOTE_HOST` | Hostname ou IP do hipervisor KVM |
| `KVM_HOSTS` | _(opcional)_ lista de hipervisores separados por espaço para um [laboratório multi-host](#multi-host-labs); padrão é apenas `REMOTE_HOST` |
| `VIRT_SRV` | URI do libvirt para o hipervisor remoto |
| `ROOT_SSH_KEY` | Conteúdo da chave pública SSH injetada nas VMs provisionadas |
| `NETWORK` | String de rede libvirt padrão |
| `REMOTE_DNS_SERVERS` | Lista de servidores DNS adicionais a atualizar, separados por espaço |
| `delay_min` | Minutos de espera entre estágios de provisionamento (aumente em hardware lento) |

### `/usr/local/lib/lab_creation/`

Módulos de biblioteca Python instalados. Atualizados ao executar `install_automation_node_scripts.sh` a partir do repositório na VM de automação.

| Arquivo | Conteúdo |
|---|---|
| `lab_creation.py` | Funções auxiliares de ciclo de vida de VM, DNS, resolução multi-host e orquestração |
| `backends.py` | Interface `VMBackend` + `LibvirtBackend` (criar/excluir/reiniciar uma VM, envio de arquivos de provisionamento) |
| `services.py` | Gerenciamento do serviço DNS |
| `spacecmd_common.py` | Automação compartilhada do SUSE Multi-Linux Manager/Uyuni (chaves de ativação, organizações, RBAC, CLM, Ansible, SCAP/CVE) usada por `install_uyuni`/`install_smlm`/`install_client_registration` |
| `primary.py` | Validação de entrada e carregamento de configuração |
| `k8s.py` | Interface de distribuição de cluster Kubernetes (RKE2/K3s) |
| `addon_common.py` | Infraestrutura de CLI compartilhada usada por cada add-on `install_*` (dispatch de `--help`/`--version`/`--schema`, validação de esquema) |

Os quatro auxiliares em bash (`lab_creation.bash`, `k8s_functions.bash`, `primary_functions.bash`, `extensions.sh`) também continuam instalados junto a estes — mantidos indefinidamente para o `install_ds389`, o único add-on que nunca foi portado para Python.

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="testing"></a>
## Testes

Cada verificação roda em seu **próprio container `podman` independente e descartável** — uma falha, travamento ou estado residual em uma não pode afetar nenhuma outra:

```shell
tests/run_tests.sh
```

Cobre sintaxe de bash e Python em toda a árvore, consistência de esquema/interface web, testes unitários com SSH simulado para cada biblioteca central e script de orquestração, e testes de regressão para bugs encontrados durante testes ao vivo. Para adicionar uma nova verificação, coloque um script executável em `tests/checks/` — ele é detectado automaticamente, sem necessidade de conexões adicionais.

Integrado a um hook de pre-commit (ative uma vez por clone, veja [Contribuindo](#contributing--developer-setup)) — roda automaticamente a cada commit e é pulado com um aviso se o `podman` não estiver instalado, em vez de bloquear o commit.

<p align="right"><a href="#top">↑ voltar ao topo</a></p>

---

<a id="contributing--developer-setup"></a>
## Contribuindo / Configuração de desenvolvimento

Veja o **[CONTRIBUTING.md](CONTRIBUTING.md)** (em inglês) para o guia completo (configuração de desenvolvimento, convenções de código, como adicionar um add-on, processo de PR). Este projeto segue o [Código de Conduta Contributor Covenant](CODE_OF_CONDUCT.md); veja o [SECURITY.md](SECURITY.md) para reportar uma vulnerabilidade. Cada push e pull request passa pelo [CI](https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml) — verificações de sintaxe/import em Python 3.11, o esquema de cada add-on, `shellcheck`, e a suíte de testes containerizada completa abaixo.

### Configuração única do git

Depois de clonar o repositório, execute:

```shell
git config core.hooksPath .githooks
```

Isso ativa os hooks em `.githooks/`, que:
- executam a [suíte de testes](#testing) completa antes de cada commit
- gerenciam a marcação de versão por script (veja abaixo)

### Como funciona o versionamento

> [!NOTE]
> Isso é totalmente gerenciado pelos hooks de git acima — você nunca edita `__LABVERSION__` manualmente.

Cada script contém o placeholder:

```python
__LABVERSION__ = "__LABVERSION__"
```

Os hooks em `.githooks/` expandem e restauram esse placeholder automaticamente:

| Hook | Gatilho | Ação |
|---|---|---|
| `post-checkout` | `git checkout` / `git switch` | Substitui `__LABVERSION__` em cada script pelo hash do último commit que tocou aquele arquivo |
| `post-merge` | `git pull` / `git merge` | Igual ao anterior |
| `post-rewrite` | `git rebase` / `git commit --amend` | Igual ao anterior |
| `pre-commit` | `git commit` | Restaura `__LABVERSION__` em qualquer script staged antes do commit ser escrito, de modo que hashes nunca são armazenados no repositório |

O resultado: cada script na sua árvore de trabalho mostra sua própria versão via `--version`, e o próprio repositório sempre guarda o placeholder limpo. Quando os scripts são instalados via `install_automation_node_scripts.sh`, a mesma substituição de hash por arquivo é aplicada no momento da instalação usando `git log -1 --format=%h`.

### Instalando os scripts na VM de automação

Da raiz do repositório na VM de automação (ou qualquer máquina com o repositório clonado):

```shell
./install_automation_node_scripts.sh
```

Isso faz backup da instalação existente (tanto seu próprio arquivo compactado com timestamp quanto, separadamente, o que quer que seu próprio processo de backup mantenha), copia cada script/biblioteca/template para seu caminho no sistema, e marca cada arquivo instalado com seu hash de versão.

### Dependências

Em tempo de execução (na VM de automação):
`python3.11`, `jq`, `ssh`, `rsync`, `nc`, `helm`, `kubectl`, `named` (BIND)

A configuração do hipervisor requer adicionalmente:
`virt-install`, `virsh`, `qemu-img`, `zypper`, imagens QCOW2 de origem em `/var/lib/libvirt/images/sources/`

Executar a [suíte de testes](#testing) requer adicionalmente:
`podman`
