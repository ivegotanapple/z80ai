# Requires GBDK-2020 4.x. Example: make GBDK=/path/to/gbdk
GBDK  ?= /opt/gbdk
LCC    = $(GBDK)/bin/lcc
MODEL ?= model.npz          # z80ai model exported with its exportmodel.py, see "make model"

SRC = main.c tiles.c lm.c lm_kernel.s model/lm_model.c \
      model/lm_w1.c model/lm_w2.c model/lm_w3.c model/lm_w4.c

# MBC1 (type 0x01), automatic ROM size, banks assigned by bankpack
gbchat.gb: $(SRC) keyboard.h tiles.h lm.h model/lm_model.h
	$(LCC) -Wl-m -Wl-j -Wm-yt0x01 -Wm-yoA -Wm-ynGBCHAT -autobank -Imodel -o $@ $(SRC)

# Regenerates the C sources of the model (weights in ROM banks)
model:
	python3 tools/gbexport.py $(MODEL) --out model

# Regenerates tiles and keyboard layout (edit LAYOUT in tools/gen_tiles.py)
tiles:
	cd tools && python3 gen_tiles.py && mv tiles.c tiles.h keyboard.h ..

# Regenerates the assembly kernel (normally not needed)
kernel:
	cd tools && python3 gen_kernel.py

clean:
	rm -f gbchat.gb gbchat.map gbchat.noi *.lst *.rel *.asm *.sym *.ihx *.o

.PHONY: model tiles kernel clean
