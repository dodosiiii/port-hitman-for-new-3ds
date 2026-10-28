TARGET := hitman47
BUILD := build-phase3
SOURCES := source
SHADERS := shaders
DATA :=
INCLUDES :=

APP_TITLE := Hitman 47
APP_DESCRIPTION := New 3DS scene preview
APP_AUTHOR := dodo16350

TOPDIR ?= $(CURDIR)
include $(DEVKITARM)/3ds_rules

empty :=
space := $(empty) $(empty)
ROOT := $(if $(PROJECT_ROOT),$(PROJECT_ROOT),$(CURDIR))
ROOT_ESCAPED := $(subst $(space),\$(space),$(ROOT))

ARCH := -march=armv6k -mtune=mpcore -mfloat-abi=hard -mtp=soft
CFLAGS := -g -Wall -Wextra -O2 -mword-relocations -ffunction-sections $(ARCH)
CFLAGS += $(INCLUDE) -D__3DS__
CXXFLAGS := $(CFLAGS) -fno-rtti -fno-exceptions -std=gnu++11
ASFLAGS := -g $(ARCH)
LDFLAGS = -specs=3dsx.specs -g $(ARCH) -Wl,-Map,$(notdir $*.map)
LIBS := -lcitro3d -lctru -lm
LIBDIRS := $(CTRULIB)

ifneq ($(PHASE3_INNER),1)

export OUTPUT := ../$(TARGET)
export TOPDIR := $(ROOT_ESCAPED)
export VPATH := $(foreach dir,$(SOURCES) $(SHADERS),$(ROOT_ESCAPED)/$(dir))
export DEPSDIR := $(ROOT_ESCAPED)/$(BUILD)
export PROJECT_ROOT := $(ROOT)

CFILES := $(foreach dir,$(SOURCES),$(notdir $(wildcard $(dir)/*.c)))
PICAFILES := $(foreach dir,$(SHADERS),$(notdir $(wildcard $(dir)/*.v.pica)))
export OFILES_SOURCES := $(CFILES:.c=.o)
export OFILES_BIN := $(PICAFILES:.v.pica=.shbin.o)
export OFILES := $(OFILES_BIN) $(OFILES_SOURCES)
export HFILES := $(PICAFILES:.v.pica=_shbin.h)
export INCLUDE := $(foreach dir,$(INCLUDES),-I$(CURDIR)/$(dir)) \
	$(foreach dir,$(LIBDIRS),-I$(dir)/include) -I.
export LIBPATHS := $(foreach dir,$(LIBDIRS),-L$(dir)/lib)
export _3DSXDEPS := $(OUTPUT).smdh

.PHONY: all clean

all: $(BUILD) $(DEPSDIR)
	@$(MAKE) --no-print-directory -C "$(BUILD)" -f ../Makefile PHASE3_INNER=1

$(BUILD):
	@mkdir -p $@

ifneq ($(DEPSDIR),$(BUILD))
$(DEPSDIR):
	@mkdir -p $@
endif

clean:
	@echo clean ...
	@rm -rf $(BUILD) $(TARGET).3dsx $(TARGET).elf $(TARGET).smdh

else

export LD := $(CC)

.PHONY: all
all: $(OUTPUT).3dsx

$(OUTPUT).3dsx: $(OUTPUT).elf $(_3DSXDEPS)

$(OFILES_SOURCES): $(HFILES)

$(OUTPUT).elf: $(OFILES)

%.shbin.o %_shbin.h: %.shbin
	$(bin2o)

main.o: ../source/main.c
	$(SILENTMSG) $(notdir $<)
	$(SILENTCMD)$(CC) -MMD -MP -MF $(DEPSDIR)/main.d $(CFLAGS) -c $< -o $@

vshader.shbin: ../shaders/vshader.v.pica
	$(SILENTMSG) $(notdir $<)
	$(SILENTCMD)picasso -o $@ $<

vshader.shbin.o vshader_shbin.h: vshader.shbin
	$(bin2o)

-include $(DEPSDIR)/*.d

endif
