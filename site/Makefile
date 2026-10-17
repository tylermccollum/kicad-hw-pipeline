.PHONY: help init kibot prepare serve build host release clean all pin-layout unpin-layout vendor-kicanvas export-template

# Everything runs in containers: only docker, git, rsync, jq, zip and python3
# are needed on the host. Local outputs live in ../generated and ../build.
REPO       := $(abspath ..)
# The KiCad project: the single *.kicad_pro next to site/ (override with PROJECT=).
PROJECT    ?= $(basename $(notdir $(wildcard $(REPO)/*.kicad_pro)))
ifneq ($(words $(PROJECT)),1)
$(error Expected exactly one .kicad_pro in $(REPO) (found: '$(PROJECT)'); set PROJECT=<name>)
endif
export PROJECT
GEN        := $(REPO)/generated
WWW        := $(REPO)/build/www
RELEASES   := $(REPO)/build/releases
HOST_ADDR  ?= 127.0.0.1
PORT       ?= 8000
HUGO_IMAGE ?= hugomods/hugo:std-go-git-0.160.1
# KiCad 10 + KiBot, pinned by digest so release outputs are reproducible.
KIBOT_IMAGE ?= ghcr.io/inti-cmnb/kicad10_auto_full@sha256:7b720dc99b2775e4f4133f06887cf9a53c85a8403438d6684ecf0916d124f52a
# URL prefix when hosted below a domain root, e.g. /<repo> on github.io.
BASE_PATH  ?=
export KIBOT_IMAGE
TAG        ?=

HUGO = docker run --rm -u $$(id -u):$$(id -g) -e HOME=/tmp -e HUGO_CACHEDIR=/cache \
	-v $(CURDIR):/src -v $(REPO)/build/hugo_cache:/cache -v $(WWW):/www -w /src

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "\033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## Set up a new project from the template: git, CI workflow, hugo.yaml, drawing layers
	scripts/init.sh

kibot: ## Run KiBot on the working tree (incl. uncommitted changes) into ../generated
	scripts/kibot.sh WORKTREE $(GEN)

prepare: ## Stage ../generated into static/ and write config_override.yaml
	scripts/prepare.sh $(GEN) $(REPO) latest $(BASE_PATH)/latest/

serve: prepare ## Live-reload dev server at http://localhost:1313/
	@mkdir -p $(REPO)/build/hugo_cache $(WWW)
	$(HUGO) -p $(HOST_ADDR):1313:1313 $(HUGO_IMAGE) sh -c 'hugo mod get && \
		hugo server -D --bind 0.0.0.0 --baseURL http://localhost:1313/ \
		--config hugo.yaml,config_override.yaml'

build: prepare ## Build the working-tree site into ../build/www/latest
	@mkdir -p $(REPO)/build/hugo_cache $(WWW)
	$(HUGO) $(HUGO_IMAGE) sh -c 'hugo mod get && \
		hugo --minify --cleanDestinationDir --config hugo.yaml,config_override.yaml -d /www/latest'
	scripts/versions.sh $(WWW)

host: ## Serve ../build/www (all built versions) on http://127.0.0.1:8000/
	@echo "Serving $(WWW) at http://$(HOST_ADDR):$(PORT)/  (Ctrl-C to stop)"
	python3 -m http.server --bind $(HOST_ADDR) --directory $(WWW) $(PORT)

release: ## Build TAG=vX.Y.Z: strict ERC/DRC, fab packages in ../build/releases/TAG, versioned site
	@[ -n "$(TAG)" ] || { echo "usage: make release TAG=v1.2.3"; exit 1; }
	STRICT=$${STRICT:-1} scripts/kibot.sh $(TAG) $(REPO)/build/tags/$(TAG)/generated
	scripts/package.sh $(REPO)/build/tags/$(TAG)/generated $(PROJECT) $(TAG) $(RELEASES)/$(TAG)
	scripts/prepare.sh $(REPO)/build/tags/$(TAG)/generated $(REPO)/build/work/$(TAG) $(TAG) $(BASE_PATH)/$(TAG)/
	@mkdir -p $(REPO)/build/hugo_cache $(WWW)
	$(HUGO) $(HUGO_IMAGE) sh -c 'hugo mod get && \
		hugo --minify --cleanDestinationDir --config hugo.yaml,config_override.yaml -d /www/$(TAG)'
	scripts/versions.sh $(WWW)

SETUP = docker run --rm -u $$(id -u):$$(id -g) -e HOME=/tmp -v $(REPO):/w -w /w $(KIBOT_IMAGE) \
	python3 site/scripts/setup_drawing_layers.py $(PROJECT).kicad_pcb $(PROJECT).kibot.yaml
CHECK_CLOSED = @! ls $(REPO)/~$(PROJECT).kicad_pcb.lck >/dev/null 2>&1 || { echo "Close the board in KiCad first"; exit 1; }

pin-layout: ## Copy the drawing layout into the board to adjust by hand (fixes the scale; close KiCad first)
	$(CHECK_CLOSED)
	$(SETUP) --pin 2>&1 | grep -v assert

unpin-layout: ## Remove pinned drawing placeholders from the board; back to automatic layout
	$(CHECK_CLOSED)
	$(SETUP) --unpin 2>&1 | grep -v assert

vendor-kicanvas: ## Refresh the vendored KiCanvas viewer from kicanvas.org
	curl -fsSL -o static/vendor/kicanvas/kicanvas.js https://kicanvas.org/kicanvas/kicanvas.js
	printf 'Vendored from https://kicanvas.org/kicanvas/kicanvas.js on %s\nsha256 %s\nRefresh with: make -C site vendor-kicanvas\n' \
		"$$(date +%F)" "$$(sha256sum static/vendor/kicanvas/kicanvas.js | cut -d' ' -f1)" > static/vendor/kicanvas/SOURCE.txt

export-template: ## Write this tooling as a blank KiCad project template to DEST (default ../build/template/hw-pipeline)
	scripts/export_template.sh $(or $(DEST),$(REPO)/build/template/hw-pipeline)

all: kibot build host ## KiBot, build, then serve locally

clean: ## Remove local site build state (keeps ../build/releases)
	rm -rf public resources config_override.yaml .hugo_build.lock \
	       $(GEN) $(WWW) $(REPO)/build/work $(REPO)/build/tags
	for d in 3D Assembly BoM Checks Diff Fabrication JLCPCB Schematic kicad assembly; do rm -rf static/$$d; done
