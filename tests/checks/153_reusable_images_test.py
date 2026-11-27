#!/usr/bin/env python3.11
# Reusable machine images — see 153_reusable_images.sh. Nothing reaches a provider: every API answer is a stub,
# and no cloud account exists anywhere in this project's tests.
import json
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

sys.path.insert(0, str(_REPO / "scripts"))

import backends  # noqa: E402
import images  # noqa: E402
import lab_image  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def died_on(fn, *args, **kwargs):
    """(died, message) for a call that should die(): libs die by SystemExit through lab_creation.die."""
    recorded = []

    def _die(msg):
        recorded.append(msg)
        raise SystemExit(msg)

    import lab_image as _cmd
    with mock.patch.object(backends, "die", side_effect=_die):
        with mock.patch.object(images, "die", side_effect=_die):
            with mock.patch.object(_cmd, "die", side_effect=_die):
                try:
                    fn(*args, **kwargs)
                except SystemExit:
                    return True, recorded[0] if recorded else ""
    return False, ""


# ── the name is the whole reuse mechanism ─────────────────────────────────────
check("an image is named after its product and version", images.name("smlm", "5.2") == "lab-in-a-box-smlm-5-2")
check("every image says it is ours", images.name("x").startswith(images.PREFIX))
check("a version's dots become dashes, since a dot is not safe in every provider's names",
      "." not in images.name("harvester", "v1.9.1"))
check("the name is lower case and nothing but letters, digits and dashes",
      images.name("SMLM Server", "5.2.1") == "lab-in-a-box-smlm-server-5-2-1")
check("a product with no version still has a name", images.name("rancher") == "lab-in-a-box-rancher")
check("two versions of one product are two images",
      images.name("smlm", "5.2") != images.name("smlm", "5.3"))
check("the name is deterministic, which is what makes a second lab find the first lab's image",
      images.name("smlm", "5.2") == images.name("smlm", "5.2"))


# ── a backend that cannot keep images is not a failure ────────────────────────
class _Plain(backends.VMBackend):
    pass


plain = _Plain()
check("a backend keeps no images unless it says it does", not plain.images_supported())
check("and imports no disks unless it says it does", not plain.image_import_supported())
check("looking for an image on such a backend answers nothing rather than failing",
      images.existing(plain, "smlm", "5.2") == "")
ok, message = died_on(images.capture, plain, "vm1", "smlm", "5.2")
check("capturing into such a backend fails, and says the VM it had nowhere to put",
      ok and "vm1" in message)
ok, message = died_on(plain.image_from_vm, "vm1", "lab-in-a-box-smlm-5-2")
check("the backend itself refuses too, naming itself", ok and "_Plain" in message)
ok, _ = died_on(plain.import_image, "lab-in-a-box-smlm-5-2", "s3://b/k")
check("and refuses to import a disk", ok)


# ── AWS: capture an instance, and import a disk out of S3 ─────────────────────
def aws_backend():
    b = backends.AWSBackend.__new__(backends.AWSBackend)
    b.region = "eu-west-1"
    b.profile = b.access_key = b.secret_key = b.session_token = ""
    b._instance_id_by_vm = {}
    b.IMAGE_INTERVAL = 0
    return b


aws = aws_backend()
check("AWS can do both routes", aws.images_supported() and aws.image_import_supported())

calls = []


def aws_api(answers):
    """Stand in for AWSBackend._aws(), answering each call by the subcommand it names. Patched onto the class, so
    the first argument is the backend itself and only the CLI arguments are recorded."""
    def _call(_self, *args, **kwargs):
        calls.append([str(a) for a in args])
        for key, answer in answers:
            if key in args:
                return answer(list(args)) if callable(answer) else answer
        return {}
    return _call


calls[:] = []
with mock.patch.object(backends.AWSBackend, "_aws",
                       aws_api([("describe-images", {"Images": [{"ImageId": "ami-dead", "State": "available"}]})])):
    found = aws.find_image("lab-in-a-box-smlm-5-2")
check("AWS finds this account's own image by name", found == "ami-dead")
check("it asks only for images this account owns, and only usable ones",
      any("--owners" in c and "self" in c for c in calls)
      and any("Name=name,Values=lab-in-a-box-smlm-5-2" in c for c in calls)
      and any("Name=state,Values=available" in c for c in calls))

calls[:] = []
with mock.patch.object(backends.AWSBackend, "_aws", aws_api([("describe-images", {"Images": []})])):
    check("and answers nothing when the account has no such image",
          aws.find_image("lab-in-a-box-smlm-5-2") == "")

states = iter([{"Images": [{"State": "pending"}]}, {"Images": [{"State": "available"}]}])
calls[:] = []
with mock.patch.object(backends.AWSBackend, "_find_instance", lambda self, vm: {"InstanceId": "i-123"}):
    with mock.patch.object(backends.AWSBackend, "_aws", aws_api([
            ("create-image", {"ImageId": "ami-new"}),
            ("describe-images", lambda a: next(states)),
            ("create-tags", {})])):
        image = aws.image_from_vm("smlm1.mydemo.lab", "lab-in-a-box-smlm-5-2")
check("AWS captures an instance into an AMI", image == "ami-new")
check("it names the image and says what built it",
      any("--name" in c and "lab-in-a-box-smlm-5-2" in c for c in calls)
      and any("Built by lab-in-a-box from smlm1.mydemo.lab" in " ".join(c) for c in calls))
check("it does not pass --no-reboot, so the image's file systems are consistent",
      not any("--no-reboot" in c for c in calls))
check("it tags the image as ours, so it is recognisable in a shared account",
      any("create-tags" in c and "Key=lab-in-a-box,Value=lab-in-a-box-smlm-5-2" in c for c in calls))
check("it waits for the AMI to stop being pending before handing it back",
      sum(1 for c in calls if "describe-images" in c) == 2)

with mock.patch.object(backends.AWSBackend, "_find_instance", lambda self, vm: None):
    ok, message = died_on(aws.image_from_vm, "gone.mydemo.lab", "lab-in-a-box-smlm-5-2")
check("capturing a VM that has no instance fails, naming the VM", ok and "gone.mydemo.lab" in message)

with mock.patch.object(backends.AWSBackend, "_find_instance", lambda self, vm: {"InstanceId": "i-123"}):
    with mock.patch.object(backends.AWSBackend, "_aws", aws_api([
            ("create-image", {"ImageId": "ami-bad"}),
            ("describe-images", {"Images": [{"State": "failed"}]}),
            ("create-tags", {})])):
        ok, message = died_on(aws.image_from_vm, "smlm1", "lab-in-a-box-smlm-5-2")
check("an AMI that ends in a failed state is reported, not returned", ok and "failed" in message)

# The import route: a disk already in S3 becomes a snapshot, then an AMI.
calls[:] = []
tasks = iter([{"ImportSnapshotTasks": [{"SnapshotTaskDetail": {"Status": "active", "Progress": "40"}}]},
              {"ImportSnapshotTasks": [{"SnapshotTaskDetail": {"Status": "completed", "SnapshotId": "snap-1"}}]}])
with mock.patch.object(backends.AWSBackend, "_aws", aws_api([
        ("import-snapshot", {"ImportTaskId": "import-ami-1"}),
        ("describe-import-snapshot-tasks", lambda a: next(tasks)),
        ("register-image", {"ImageId": "ami-imported"}),
        ("describe-images", {"Images": [{"State": "available"}]})])):
    imported = aws.import_image("lab-in-a-box-harvester-v1-9-1", "s3://lab-images/harvester.raw")
check("AWS imports a disk out of S3 and registers it as an AMI", imported == "ami-imported")
container = next(json.loads(c[c.index("--disk-container") + 1]) for c in calls if "--disk-container" in c)
check("the disk container names the bucket and key it was given",
      container["UserBucket"] == {"S3Bucket": "lab-images", "S3Key": "harvester.raw"})
check("and declares the disk raw, which is what this project builds", container["Format"] == "raw")
check("it waits for the import to complete before registering anything",
      sum(1 for c in calls if "describe-import-snapshot-tasks" in c) == 2)
mapping = next(json.loads(c[c.index("--block-device-mappings") + 1]) for c in calls
               if "--block-device-mappings" in c)
check("the registered AMI boots the imported snapshot as its root disk",
      mapping[0]["Ebs"]["SnapshotId"] == "snap-1")
check("and boots it the way this project builds images, UEFI", any("uefi" in c for c in calls))

ok, message = died_on(aws.import_image, "x", "/local/disk.raw")
check("a disk that is not in S3 is refused, since EC2 reads the disk itself", ok and "s3://" in message)
ok, _ = died_on(aws.import_image, "x", "s3://bucket-only")
check("an S3 location with no key is refused too", ok)


# ── Hetzner: capture a server; it cannot import a disk at all ─────────────────
def hetzner_backend():
    b = backends.HetznerBackend.__new__(backends.HetznerBackend)
    b.token = "t"
    b.IMAGE_INTERVAL = 0
    return b


hz = hetzner_backend()
check("Hetzner can capture a server", hz.images_supported())
check("but cannot import a disk, because its API has no such operation",
      not hz.image_import_supported())
ok, message = died_on(hz.import_image, "lab-in-a-box-harvester-v1-9-1", "s3://b/k")
check("asking it to import one fails rather than appearing to work",
      ok and "HetznerBackend" in message)

api_calls = []


def hz_api(answers):
    def _call(self, method, path, body=None):
        api_calls.append((method, path, body))
        for key, answer in answers:
            if key in path:
                return answer(path) if callable(answer) else answer
        return {}
    return _call


api_calls[:] = []
with mock.patch.object(backends.HetznerBackend, "_api",
                       hz_api([("/images", {"images": [{"id": 4711, "status": "available"}]})])):
    check("Hetzner finds its own snapshot and answers the id create_vm takes",
          hz.find_image("lab-in-a-box-smlm-5-2") == "4711")
check("it looks the snapshot up by label, because a Hetzner snapshot has no name of its own",
      any("label_selector=lab-in-a-box%3Dlab-in-a-box-smlm-5-2" in p for _m, p, _b in api_calls))
check("and only considers snapshots that are usable",
      any("type=snapshot" in p and "status=available" in p for _m, p, _b in api_calls))

api_calls[:] = []
hz_states = iter([{"image": {"status": "creating"}}, {"image": {"status": "available"}}])
with mock.patch.object(backends.HetznerBackend, "_find_server", lambda self, vm: {"id": 99}):
    with mock.patch.object(backends.HetznerBackend, "_api", hz_api([
            ("actions/create_image", {"image": {"id": 4712}}),
            ("/images/", lambda p: next(hz_states))])):
        captured = hz.image_from_vm("smlm1.mydemo.lab", "lab-in-a-box-smlm-5-2")
check("Hetzner captures a server into a snapshot", captured == "4712")
body = next(b for _m, p, b in api_calls if "create_image" in p)
check("the snapshot carries the name as its label and its description",
      body["labels"] == {"lab-in-a-box": "lab-in-a-box-smlm-5-2"}
      and body["description"] == "lab-in-a-box-smlm-5-2")
check("and is a snapshot, not one of Hetzner's own backups", body["type"] == "snapshot")
check("it waits for the snapshot to become available",
      sum(1 for _m, p, _b in api_calls if p.startswith("/images/")) == 2)

with mock.patch.object(backends.HetznerBackend, "_find_server", lambda self, vm: None):
    ok, message = died_on(hz.image_from_vm, "gone.mydemo.lab", "lab-in-a-box-smlm-5-2")
check("capturing a VM with no server fails, naming the VM", ok and "gone.mydemo.lab" in message)


# ── reuse is what the whole thing is for ──────────────────────────────────────
with mock.patch.object(backends.AWSBackend, "_aws",
                       aws_api([("describe-images", {"Images": [{"ImageId": "ami-kept", "State": "available"}]})])):
    check("an image already built is found and reused instead of built again",
          images.existing(aws, "smlm", "5.2") == "ami-kept")
with mock.patch.object(backends.AWSBackend, "_aws", aws_api([("describe-images", {"Images": []})])):
    check("and nothing is reused when nothing was built", images.existing(aws, "smlm", "5.2") == "")

# ── every backend declares what it can do, and the declarations match the APIs ─
# Verified against each provider's own specification or SDK source, not from memory: Hetzner's /images carries
# only GET, and OVHcloud's image endpoint is a GET too, so neither can take a disk built elsewhere.
CAPTURE = {"aws", "gcp", "alibaba", "hetzner", "scaleway", "upcloud", "ovhcloud", "exoscale"}
IMPORT = {"aws", "gcp", "alibaba", "scaleway", "upcloud", "exoscale"}

for backend_name, cls in sorted(backends.BACKENDS.items()):
    check("{} declares whether it can capture a VM".format(backend_name),
          cls.images_supported(cls) == (backend_name in CAPTURE))
    check("{} declares whether it can import a disk".format(backend_name),
          cls.image_import_supported(cls) == (backend_name in IMPORT))

check("every cloud backend can capture a VM, which is why that route is the default",
      all(backends.BACKENDS[n].images_supported(backends.BACKENDS[n]) for n in CAPTURE))
check("two providers cannot import a disk at all, which is why the capability is declared and not assumed",
      not backends.BACKENDS["hetzner"].image_import_supported(backends.BACKENDS["hetzner"])
      and not backends.BACKENDS["ovhcloud"].image_import_supported(backends.BACKENDS["ovhcloud"]))
check("a backend that can import a disk can also capture a VM, since importing is the harder one",
      IMPORT.issubset(CAPTURE))

# Each one has to actually implement what it declares, rather than inherit the base class's refusal.
for backend_name in sorted(CAPTURE):
    cls = backends.BACKENDS[backend_name]
    check("{} implements find_image itself".format(backend_name),
          cls.find_image is not backends.VMBackend.find_image)
    check("{} implements image_from_vm itself".format(backend_name),
          cls.image_from_vm is not backends.VMBackend.image_from_vm)
for backend_name in sorted(IMPORT):
    cls = backends.BACKENDS[backend_name]
    check("{} implements import_image itself".format(backend_name),
          cls.import_image is not backends.VMBackend.import_image)
for backend_name in sorted(CAPTURE - IMPORT):
    cls = backends.BACKENDS[backend_name]
    check("{} leaves import_image refusing, rather than half-implementing it".format(backend_name),
          cls.import_image is backends.VMBackend.import_image)

# ── the command that drives both routes ───────────────────────────────────────
check("it offers both routes and a way to look first", set(lab_image.ACTIONS) == {"find", "capture", "import"})
help_text = lab_image._HELP_TEXT
check("the help says capture is the usual route and every backend can do it",
      "usual route" in help_text and "every backend can do it" in help_text)
check("the help says why the import route exists and that not every backend has it",
      "cannot be installed onto a running OS" in help_text and "Fewer backends" in help_text)
check("the help says what the image ends up called, since that is how a later lab finds it",
      "lab-in-a-box-<product>-<version>" in help_text)
check("find reports by its exit code, so a script can branch on it", "Exit 0 when one" in help_text)

installer = (_REPO / "install_automation_node_scripts.sh").read_text()
check("the command is installed onto the automation node", "lab_image.py" in installer)

# A backend that cannot import a disk has to say so rather than appear to work, because the alternative is an
# operator waiting on an upload that will never be registered.
argv = ["lab_image.py", "import", "lab.json", "vm1", "harvester", "v1.9.1", "s3://b/k"]
with mock.patch.object(lab_image.sys, "argv", argv):
    with mock.patch.object(lab_image, "_backend", lambda *a, **k: hetzner_backend()):
        ok, message = died_on(lab_image.main)
check("importing into a backend that cannot do it fails, and says what to do instead",
      ok and "HetznerBackend" in message and "capture" in message)

# ── a lab definition says which nodes keep an image ───────────────────────────
lab = {"common": {"keep_image": "smlm", "keep_image_version": "5.2"},
       "nodes": {"a.x.lab": {}, "b.x.lab": {"keep_image": "rancher"},
                 "c.x.lab": {"keep_image": "rancher", "keep_image_version": "2.9"},
                 "d.x.lab": {"keep_image": ""}}}
check("a node inherits common's product and version", images.product_of(lab, "a.x.lab") == ("smlm", "5.2"))
check("a node's own product wins", images.product_of(lab, "b.x.lab")[0] == "rancher")
check("and does not inherit another product's version, which would misname the image",
      images.product_of(lab, "b.x.lab") == ("rancher", "")
      and images.name(*images.product_of(lab, "b.x.lab")) == "lab-in-a-box-rancher")
check("a node naming both keeps both", images.product_of(lab, "c.x.lab") == ("rancher", "2.9"))
check("an empty value on a node falls back to common, as every other field does",
      images.product_of(lab, "d.x.lab") == ("smlm", "5.2"))
check("a lab that keeps no image answers nothing",
      images.product_of({"nodes": {"a": {}}}, "a") == ("", ""))

# The two halves have to agree on where the fields are read from, or a node would be built from one image and
# captured into another.
schema = (_REPO / "libs" / "lab_schema_base.py").read_text()
check("both fields are in the lab schema", '"keep_image"' in schema and '"keep_image_version"' in schema)
check("the schema says the image is reused as well as kept",
      "built from that image whenever it already exists" in schema)
setup_vm = (_REPO / "scripts" / "setup_vm.py").read_text()
check("creating a VM looks for the image before it chooses what to boot",
      "images.product_of(definition, vm_name)" in setup_vm
      and setup_vm.index("images.existing") < setup_vm.index("backend.copy_vm_image(env.get"))
check("a kept image replaces SOURCE_IMAGE", 'env["SOURCE_IMAGE"] = kept' in setup_vm)
check("and drops the checksum with it, since a provider's image is not a file to verify",
      'env["SOURCE_SHA256"] = ""' in setup_vm)

setup_lab = (_REPO / "scripts" / "setup_lab.py").read_text()
check("the lab captures its images in a phase of its own", "def phase_keep_images(" in setup_lab)
check("which runs after the add-ons and the post_commands, so the image is the finished node",
      setup_lab.index("phase_post_commands(definition)\n    phase_keep_images(") > 0)
check("a node whose own creation failed is not captured", "failed_nodes" in
      setup_lab.split("def phase_keep_images(")[1].split("def phase_post_commands")[0])
keep_phase = setup_lab.split("def phase_keep_images(")[1].split("\ndef ")[0]
check("a node whose image is already there is skipped, which is what makes a second run a no-op",
      "if images.existing(backend, product, version):" in keep_phase and "continue" in keep_phase)
check("a backend that cannot keep images warns and carries on rather than failing the run",
      "images_supported()" in keep_phase and "lc.warn(" in keep_phase)
check("a provider refusing at the last step is an error in the summary, not the end of the run",
      "_report.add_error(msg)" in keep_phase)

# ── the library states its own contract ───────────────────────────────────────
source = (_REPO / "libs" / "images.py").read_text()
check("it says the capture route is the default", "capture  The default" in source)
check("it says why the import route exists at all", "cannot be installed onto a running OS" in source)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all reusable-image checks passed")
