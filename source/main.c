#include <3ds.h>
#include <citro3d.h>
#include <tex3ds.h>
#include <3ds/services/irrst.h>

#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "vshader_shbin.h"

#define DATA_ROOT "sdmc:/3ds/hitman47/data"
#define PACKAGE_PATH "sdmc:/3ds/hitman47/data.c47p"
#define DEFAULT_MISSION "C1_1"
#define MAX_MISSIONS 64
#define MIN_VIEW_DIST 3000.0f
#define MAX_VIEW_LIMIT 22000.0f
#define NO_MODEL 0xFFFFFFFFu
#define NO_TEXTURE 0xFFFFFFFFu
#define MODEL_HEADER_SIZE 80u
#define MODEL_BATCH_SIZE 24u
#define MODEL_VERTEX_SIZE 18u
#define MAX_FILE_SIZE (64u * 1024u * 1024u)
#define PACKAGE_HEADER_SIZE 24u
#define PACKAGE_ENTRY_SIZE 12u
#define MAX_PACKAGE_INDEX_SIZE (16u * 1024u * 1024u)
#define CLEAR_COLOR 0x181820FF

/* Tampon de commandes GPU 1 Mo */
#define GPU_CMDBUF_SIZE 0x100000

/* Plafond d'appels de dessin par frame */
#define MAX_FRAME_DRAW_CALLS 900

/* Distance d'affichage par defaut : 160 metres (16000 unites), reglable en jeu (L/R) */
#define DEFAULT_VIEW_DIST 16000.0f

#define NEAR_PLANE 25.0f
#define FAR_PLANE 24000.0f

#define MAX_TEXTURES 2048
#define MAX_MODELS 8192
#define MAX_SCENE_OBJECTS 70000

/* Memoire lineaire (GPU) : seuils en octets */
#define LOW_LINEAR_EVICT (10u * 1024u * 1024u)
#define LOW_LINEAR_NOSTREAM (5u * 1024u * 1024u)

/* Physique du joueur (unites = 1/100 m) */
#define STEP_UP 70.0f
#define GRAVITY 2500.0f

/*
 * Mettre a 1 si les textures apparaissent a l'envers (haut/bas inverse).
 */
#define TEXTURE_FLIP_V 0

/* 1 = ne pas dessiner les volumes d'aide (Ground type 22, InsideBound type 28) */
#define HIDE_HELPER_TYPES 1

/* Rend une coordonnee UV exploitable par le GPU (NaN / valeurs enormes) */
static float sanitize_uv(float v)
{
	if (!isfinite(v))
		return 0.0f;
	if (v > 64.0f || v < -64.0f)
		v = fmodf(v, 1.0f);
	return v;
}

#define DISPLAY_TRANSFER_FLAGS \
	(GX_TRANSFER_FLIP_VERT(0) | GX_TRANSFER_OUT_TILED(0) | \
	 GX_TRANSFER_RAW_COPY(0) | \
	 GX_TRANSFER_IN_FORMAT(GX_TRANSFER_FMT_RGBA8) | \
	 GX_TRANSFER_OUT_FORMAT(GX_TRANSFER_FMT_RGB8) | \
	 GX_TRANSFER_SCALING(GX_TRANSFER_SCALE_NO))

typedef struct
{
	float position[3];
	float texcoord[2];
	float color[4];
} Vertex;

typedef struct
{
	uint32_t texture_id;
	uint32_t light_texture_id;
	uint32_t texture_kind;
	uint32_t first_index;
	uint32_t index_count;
	uint32_t first_vertex;
	uint32_t face_flags;
	int texture_slot;
} DrawBatch;

/* state : 0 = libre, 1 = chargee, 2 = echec permanent (cache negatif),
 * 3 = echec d'import a reessayer (memoire fragmentee, lecture SD...) */
typedef struct
{
	uint32_t texture_id;
	uint32_t texture_kind;
	C3D_Tex texture;
	int state;
	int refs;
	int fail_count;
	uint32_t last_used_frame;
} LoadedTexture;

typedef struct
{
	uint8_t *records;
	char *strings;
	uint32_t count;
	uint32_t archive_size;
	FILE *file;
} DataPackage;

typedef struct
{
	void *vertices;
	void *indices;
	uint32_t vertex_count;
	uint32_t index_count;
	uint32_t batch_count;
	DrawBatch *batches;
	int loaded;
	int failed;
	uint32_t retry_frame;
} Model;

typedef struct
{
	uint32_t id;
	uint32_t type;
	uint32_t model_id;
	float position[3];
	C3D_Mtx world_matrix;
	float bounds[6];
	float center[3];
	float radius;
} SceneObject;

typedef struct
{
	uint32_t obj_index;
	float dist_sq;
} VisibleObject;

typedef struct
{
	float position[3];
	float yaw;
	float pitch;
} Camera;

typedef enum
{
	CAMERA_MODE_THIRD_PERSON = 0,
	CAMERA_MODE_FREE = 1
} CameraMode;

typedef struct
{
	float position[3];
	float yaw;
	float vy;
	uint32_t model_id;
	int scene_obj_index;
} Player;

static Player s_player;
static CameraMode s_camera_mode = CAMERA_MODE_THIRD_PERSON;
static float s_cam_orbit_dist = 360.0f;
static float s_cam_orbit_pitch = 0.20f;
static float s_cam_orbit_yaw = 0.0f;
static float s_cam_target_y = 0.0f;
static C3D_FogLut s_fog_lut;
static int s_fog_enabled = 1;
static float s_view_dist = DEFAULT_VIEW_DIST;

/* Missions */
static char s_mission_name[48] = DEFAULT_MISSION;
static char s_mission_names[MAX_MISSIONS][48];
static int s_mission_count = 0;
static int s_spawn_fallback = 0;

static DVLB_s *s_shader_binary;
static shaderProgram_s s_shader_program;
static C3D_Mtx s_projection;
static C3D_RenderTarget *s_top_target;
static int s_projection_location = -1;
static int s_model_view_location = -1;

static C3D_Tex s_white_texture;
static int s_white_texture_initialized = 0;

static Model s_models[MAX_MODELS];
static LoadedTexture s_texture_cache[MAX_TEXTURES];
static uint32_t s_texture_cache_count = 0;
static SceneObject s_scene_objects[MAX_SCENE_OBJECTS];
static uint32_t s_scene_object_count = 0;
static VisibleObject s_visible_objects[MAX_SCENE_OBJECTS];
static Camera s_camera;
static uint32_t s_mission_index = 0;

static uint8_t *s_tex_index_data = NULL;
static size_t s_tex_index_size = 0;

static uint32_t s_drawn_objects = 0;
static uint32_t s_drawn_triangles = 0;
static uint32_t s_draw_calls = 0;

/* Statistiques / debug */
static uint32_t s_frame = 0;
static uint32_t s_tex_loaded = 0;
static uint32_t s_tex_failed = 0;
static uint32_t s_models_loaded = 0;
static uint32_t s_models_failed = 0;
static uint32_t s_fail_logged = 0;

static DataPackage s_package;
static FILE *s_log;
static char s_error[256];
static int s_citro3d_initialized;
static int s_package_state;

static void log_message(const char *message);
static void evict_far_models(int max_unloads);

static uint16_t read_u16(const uint8_t *data)
{
	return (uint16_t)(data[0] | ((uint16_t)data[1] << 8));
}

static uint32_t read_u32(const uint8_t *data)
{
	return (uint32_t)data[0]
		| ((uint32_t)data[1] << 8)
		| ((uint32_t)data[2] << 16)
		| ((uint32_t)data[3] << 24);
}

static float read_f32(const uint8_t *data)
{
	uint32_t bits = read_u32(data);
	float value;
	memcpy(&value, &bits, sizeof(value));
	return value;
}

static void log_message(const char *message)
{
	if (s_log)
	{
		fprintf(s_log, "%s\n", message);
		fflush(s_log);
	}
}

/* Log limite pour ne pas remplir la SD si tout echoue */
static void log_failure(const char *format, uint32_t a, uint32_t b)
{
	char line[200];
	if (s_fail_logged >= 200)
		return;
	++s_fail_logged;
	snprintf(line, sizeof(line), format, (unsigned long)a, (unsigned long)b);
	log_message(line);
}

static int package_path_is_valid(const char *path)
{
	const char *segment = path;

	if (!path || !*path || *path == '/' || strchr(path, '\\'))
		return 0;
	for (;;)
	{
		const char *separator = strchr(segment, '/');
		size_t length = separator ? (size_t)(separator - segment) : strlen(segment);
		if (length == 0 || (length == 1 && segment[0] == '.')
			|| (length == 2 && segment[0] == '.' && segment[1] == '.'))
			return 0;
		if (!separator)
			return 1;
		segment = separator + 1;
	}
}

static void package_set_error(const char *message)
{
	snprintf(s_error, sizeof(s_error), "%s", message);
	log_message(s_error);
}

static int package_init(void)
{
	uint8_t header[PACKAGE_HEADER_SIZE];
	long file_size;
	uint32_t entry_count;
	uint32_t records_offset;
	uint32_t strings_offset;
	uint32_t strings_size;
	uint32_t index;
	const char *previous_path = NULL;
	size_t index_size;

	if (s_package_state != 0)
		return s_package_state;

	s_package.file = fopen(PACKAGE_PATH, "rb");
	if (!s_package.file)
	{
		s_package_state = 2;
		return s_package_state;
	}

	if (fseek(s_package.file, 0, SEEK_END) != 0
		|| (file_size = ftell(s_package.file)) < (long)PACKAGE_HEADER_SIZE
		|| (unsigned long)file_size > 0x7FFFFFFFul
		|| fseek(s_package.file, 0, SEEK_SET) != 0
		|| fread(header, 1, sizeof(header), s_package.file) != sizeof(header))
		goto invalid;

	entry_count = read_u32(header + 8);
	records_offset = read_u32(header + 12);
	strings_offset = read_u32(header + 16);
	strings_size = read_u32(header + 20);
	if (memcmp(header, "C47P", 4) != 0 || read_u16(header + 4) != 1
		|| read_u16(header + 6) != 0
		|| records_offset != PACKAGE_HEADER_SIZE
		|| entry_count > ((uint32_t)file_size - PACKAGE_HEADER_SIZE)
			/ PACKAGE_ENTRY_SIZE
		|| strings_offset != PACKAGE_HEADER_SIZE
			+ entry_count * PACKAGE_ENTRY_SIZE
		|| strings_size > MAX_PACKAGE_INDEX_SIZE
		|| strings_offset > (uint32_t)file_size
		|| strings_size > (uint32_t)file_size - strings_offset)
		goto invalid;

	index_size = (size_t)entry_count * PACKAGE_ENTRY_SIZE;
	s_package.records = malloc(index_size ? index_size : 1);
	s_package.strings = malloc(strings_size ? strings_size : 1);
	if (!s_package.records || !s_package.strings
		|| fseek(s_package.file, (long)records_offset, SEEK_SET) != 0
		|| fread(s_package.records, 1, index_size, s_package.file) != index_size
		|| fread(s_package.strings, 1, strings_size, s_package.file) != strings_size)
		goto invalid;

	s_package.count = entry_count;
	s_package.archive_size = (uint32_t)file_size;
	for (index = 0; index < entry_count; ++index)
	{
		const uint8_t *record = s_package.records + index * PACKAGE_ENTRY_SIZE;
		uint32_t path_offset = read_u32(record);
		uint32_t data_offset = read_u32(record + 4);
		uint32_t data_size = read_u32(record + 8);
		const char *entry_path;
		const char *path_end;

		if (path_offset >= strings_size
			|| data_offset < strings_offset + strings_size
			|| data_offset > s_package.archive_size
			|| data_size > s_package.archive_size - data_offset)
			goto invalid;
		entry_path = s_package.strings + path_offset;
		path_end = memchr(entry_path, '\0', strings_size - path_offset);
		if (!path_end || !package_path_is_valid(entry_path)
			|| (previous_path && strcmp(previous_path, entry_path) >= 0))
			goto invalid;
		previous_path = entry_path;
	}

	s_package_state = 1;
	log_message("Paquet data.c47p charge");
	return s_package_state;

invalid:
	if (s_package.file)
		fclose(s_package.file);
	s_package.file = NULL;
	free(s_package.records);
	free(s_package.strings);
	memset(&s_package, 0, sizeof(s_package));
	s_package_state = -1;
	package_set_error("Le paquet data.c47p est invalide ou tronque.");
	return s_package_state;
}

static int package_read_file(const char *relative_path,
	uint8_t **contents, size_t *size)
{
	uint32_t lower = 0;
	uint32_t upper = s_package.count;
	uint8_t *buffer;

	while (lower < upper)
	{
		uint32_t middle = lower + (upper - lower) / 2;
		const uint8_t *record = s_package.records + middle * PACKAGE_ENTRY_SIZE;
		const char *entry_path = s_package.strings + read_u32(record);
		int comparison = strcmp(relative_path, entry_path);
		if (comparison == 0)
		{
			uint32_t data_offset = read_u32(record + 4);
			uint32_t data_size = read_u32(record + 8);
			if (data_size > MAX_FILE_SIZE
				|| fseek(s_package.file, (long)data_offset, SEEK_SET) != 0)
				return 0;
			buffer = malloc(data_size ? data_size : 1);
			if (!buffer)
				return 0;
			if (data_size && fread(buffer, 1, data_size, s_package.file) != data_size)
			{
				free(buffer);
				return 0;
			}
			*contents = buffer;
			*size = data_size;
			return 1;
		}
		if (comparison < 0)
			upper = middle;
		else
			lower = middle + 1;
	}

	return 0;
}

static int read_file(const char *path, uint8_t **contents, size_t *size)
{
	FILE *file;
	long file_size;
	uint8_t *buffer;
	int package_state = package_init();

	if (package_state < 0)
		return 0;
	if (package_state == 1)
	{
		size_t root_length = strlen(DATA_ROOT);
		if (strncmp(path, DATA_ROOT, root_length) != 0 || path[root_length] != '/')
			return 0;
		return package_read_file(path + root_length + 1, contents, size);
	}

	file = fopen(path, "rb");
	if (!file)
		return 0;
	if (fseek(file, 0, SEEK_END) != 0 || (file_size = ftell(file)) < 0
		|| (unsigned long)file_size > MAX_FILE_SIZE
		|| fseek(file, 0, SEEK_SET) != 0)
	{
		fclose(file);
		return 0;
	}

	buffer = malloc(file_size ? (size_t)file_size : 1);
	if (!buffer)
	{
		fclose(file);
		return 0;
	}
	if (file_size && fread(buffer, 1, (size_t)file_size, file) != (size_t)file_size)
	{
		free(buffer);
		fclose(file);
		return 0;
	}
	fclose(file);
	*contents = buffer;
	*size = (size_t)file_size;
	return 1;
}

static int read_string(const uint8_t *table, size_t table_size,
	size_t offset, char *output, size_t output_size)
{
	const uint8_t *end;
	size_t length;

	if (offset >= table_size)
		return 0;
	end = memchr(table + offset, 0, table_size - offset);
	if (!end)
		return 0;
	length = (size_t)(end - (table + offset));
	if (length >= output_size)
		return 0;
	memcpy(output, table + offset, length);
	output[length] = '\0';
	return 1;
}

static int find_mission_index(uint32_t *mission_index)
{
	char path[128];
	uint8_t *data = NULL;
	size_t size = 0;
	uint32_t mission_count;
	uint32_t strings_size;
	uint32_t strings_offset;
	uint32_t index;
	int found = 0;

	snprintf(path, sizeof(path), "%s/index.bin", DATA_ROOT);
	if (!read_file(path, &data, &size) || size < 20
		|| memcmp(data, "C47I", 4) != 0 || read_u16(data + 4) != 1)
		goto done;

	mission_count = read_u16(data + 6);
	strings_size = read_u32(data + 12);
	strings_offset = read_u32(data + 16);
	if (mission_count > (size - 20) / 36
		|| strings_offset != 20 + mission_count * 36
		|| strings_offset > size || strings_size != size - strings_offset)
		goto done;

	for (index = 0; index < mission_count; ++index)
	{
		const uint8_t *record = data + 20 + index * 36;
		char name[64];
		if (read_string(data + strings_offset, strings_size,
			read_u32(record), name, sizeof(name)) && strcmp(name, s_mission_name) == 0)
		{
			*mission_index = index;
			found = 1;
			break;
		}
	}

done:
	free(data);
	return found;
}

/* Lit la liste des missions de index.bin (l'ordre = l'indice de mission) */
static int load_mission_list(void)
{
	char path[128];
	uint8_t *data = NULL;
	size_t size = 0;
	uint32_t mission_count;
	uint32_t strings_size;
	uint32_t strings_offset;
	uint32_t index;

	s_mission_count = 0;
	snprintf(path, sizeof(path), "%s/index.bin", DATA_ROOT);
	if (!read_file(path, &data, &size) || size < 20
		|| memcmp(data, "C47I", 4) != 0 || read_u16(data + 4) != 1)
		goto done;

	mission_count = read_u16(data + 6);
	strings_size = read_u32(data + 12);
	strings_offset = read_u32(data + 16);
	if (mission_count > (size - 20) / 36
		|| strings_offset != 20 + mission_count * 36
		|| strings_offset > size || strings_size != size - strings_offset)
		goto done;

	for (index = 0; index < mission_count && index < MAX_MISSIONS; ++index)
	{
		const uint8_t *record = data + 20 + index * 36;
		if (!read_string(data + strings_offset, strings_size,
			read_u32(record), s_mission_names[index], sizeof(s_mission_names[index])))
			snprintf(s_mission_names[index], sizeof(s_mission_names[index]),
				"mission_%lu", (unsigned long)index);
		s_mission_count = (int)index + 1;
	}

done:
	free(data);
	return s_mission_count;
}

/*
 * Cherche le nom du fichier de texture. Premiere passe : mission exacte.
 * Deuxieme passe : n'importe quelle mission (textures partagees).
 */
static int find_texture_asset(uint32_t mission_index, uint32_t texture_id,
	uint32_t texture_kind, char *asset_name, size_t asset_name_size)
{
	char path[128];
	uint32_t count;
	uint32_t strings_offset;
	uint32_t strings_size;
	uint32_t index;
	int pass;

	if (!s_tex_index_data)
	{
		snprintf(path, sizeof(path), "%s/tex/index.bin", DATA_ROOT);
		if (!read_file(path, &s_tex_index_data, &s_tex_index_size))
			return 0;
	}

	if (s_tex_index_size < 20 || memcmp(s_tex_index_data, "C47T", 4) != 0
		|| read_u16(s_tex_index_data + 4) != 1)
		return 0;

	count = read_u32(s_tex_index_data + 8);
	strings_offset = read_u32(s_tex_index_data + 12);
	strings_size = read_u32(s_tex_index_data + 16);
	if (count > (s_tex_index_size - 20) / 36
		|| strings_offset != 20 + count * 36
		|| strings_offset > s_tex_index_size
		|| strings_size != s_tex_index_size - strings_offset)
		return 0;

	for (pass = 0; pass < 2; ++pass)
	{
		for (index = 0; index < count; ++index)
		{
			const uint8_t *record = s_tex_index_data + 20 + index * 36;
			if ((pass == 1 || read_u32(record) == mission_index)
				&& read_u32(record + 4) == texture_id
				&& read_u32(record + 20) == texture_kind)
			{
				if (read_string(s_tex_index_data + strings_offset, strings_size,
					read_u32(record + 24), asset_name, asset_name_size))
					return 1;
			}
		}
	}

	return 0;
}

/* Libere les textures qui ne sont plus referencees (refs == 0) par ordre LRU.
 * Permet d'eviter la suppression immediate et les clignotements blanc. */
static int evict_unreferenced_textures(int max_evicts)
{
	int freed = 0;
	uint32_t i;
	while (freed < max_evicts)
	{
		int oldest_slot = -1;
		uint32_t oldest_frame = 0xFFFFFFFFu;
		for (i = 0; i < s_texture_cache_count; ++i)
		{
			LoadedTexture *t = &s_texture_cache[i];
			if (t->state == 1 && t->refs == 0)
			{
				if (t->last_used_frame < oldest_frame)
				{
					oldest_frame = t->last_used_frame;
					oldest_slot = (int)i;
				}
			}
		}
		if (oldest_slot < 0)
			break;
		C3D_TexDelete(&s_texture_cache[oldest_slot].texture);
		s_texture_cache[oldest_slot].state = 0;
		s_texture_cache[oldest_slot].refs = 0;
		if (s_tex_loaded > 0)
			s_tex_loaded--;
		freed++;
	}
	return freed;
}

/* transient = 1 si l'echec vient d'un manque de memoire (on reessaiera) */
static int load_texture(uint32_t mission_index, uint32_t texture_id,
	uint32_t texture_kind, LoadedTexture *texture, int *transient)
{
	char asset_name[128];
	char path[256];
	uint8_t *file_data = NULL;
	size_t file_size = 0;
	Tex3DS_Texture imported;

	*transient = 0;
	if (texture_id == NO_TEXTURE)
		return 0;

	if (!find_texture_asset(mission_index, texture_id, texture_kind,
		asset_name, sizeof(asset_name)))
	{
		if (texture_kind != 1 || !find_texture_asset(mission_index,
			texture_id, 2, asset_name, sizeof(asset_name)))
		{
			log_failure("tex %lu (kind %lu): absente de l'index", texture_id, texture_kind);
			return 0;
		}
	}

	/* Les sous-dossiers sont autorises, mais pas ".." ni "\\" */
	if (!package_path_is_valid(asset_name))
	{
		log_failure("tex %lu (kind %lu): chemin invalide", texture_id, texture_kind);
		return 0;
	}

	snprintf(path, sizeof(path), "%s/tex/%s", DATA_ROOT, asset_name);
	if (!read_file(path, &file_data, &file_size))
	{
		log_failure("tex %lu (kind %lu): fichier illisible/absent", texture_id, texture_kind);
		return 0;
	}

	if (linearSpaceFree() < LOW_LINEAR_NOSTREAM)
		evict_unreferenced_textures(8);

	imported = Tex3DS_TextureImport(file_data, file_size,
		&texture->texture, NULL, false);
	if (!imported && linearSpaceFree() < LOW_LINEAR_EVICT)
	{
		/* Memoire fragmentee : purger les textures inactives et reessayer une fois */
		if (evict_unreferenced_textures(8) > 0)
		{
			imported = Tex3DS_TextureImport(file_data, file_size,
				&texture->texture, NULL, false);
		}
	}
	free(file_data);
	if (!imported)
	{
		if (linearSpaceFree() < LOW_LINEAR_NOSTREAM)
		{
			*transient = 1;
			log_failure("tex %lu (kind %lu): memoire GPU pleine", texture_id, texture_kind);
		}
		else
		{
			/* Memoire fragmentee ou t3x invalide : on reessaie quelques fois */
			*transient = 2;
			log_failure("tex %lu (kind %lu): import t3x refuse (reessai)", texture_id, texture_kind);
		}
		return 0;
	}
	Tex3DS_TextureFree(imported);

	C3D_TexSetFilter(&texture->texture, GPU_LINEAR, GPU_LINEAR);
	C3D_TexSetWrap(&texture->texture, GPU_REPEAT, GPU_REPEAT);
	return 1;
}

/* Retourne le slot (refs incremente) ou -1. *transient = 1 : reessayer plus tard. */
static int get_texture_slot(uint32_t mission_index, uint32_t texture_id,
	uint32_t texture_kind, int *transient)
{
	uint32_t i;
	int free_slot = -1;
	int retry_slot = -1;
	int load_status = 0;
	int prior_fails = 0;
	LoadedTexture *slot;

	*transient = 0;
	if (texture_id == NO_TEXTURE)
		return -1;

	for (i = 0; i < s_texture_cache_count; ++i)
	{
		LoadedTexture *t = &s_texture_cache[i];
		if (t->state == 0)
		{
			if (free_slot < 0)
				free_slot = (int)i;
			continue;
		}
		if (t->texture_id == texture_id && t->texture_kind == texture_kind)
		{
			if (t->state == 1)
			{
				t->refs++;
				t->last_used_frame = s_frame;
				return (int)i;
			}
			if (t->state == 3)
			{
				retry_slot = (int)i;
				break;
			}
			return -1; /* echec permanent deja connu */
		}
	}

	if (retry_slot >= 0)
	{
		free_slot = retry_slot;
		prior_fails = s_texture_cache[retry_slot].fail_count;
	}
	else if (free_slot < 0)
	{
		if (s_texture_cache_count >= MAX_TEXTURES)
		{
			if (evict_unreferenced_textures(4) > 0)
			{
				for (i = 0; i < s_texture_cache_count; ++i)
				{
					if (s_texture_cache[i].state == 0)
					{
						free_slot = (int)i;
						break;
					}
				}
			}
			if (free_slot < 0)
			{
				*transient = 1;
				return -1;
			}
		}
		else
		{
			free_slot = (int)s_texture_cache_count++;
		}
		memset(&s_texture_cache[free_slot], 0, sizeof(s_texture_cache[free_slot]));
	}

	slot = &s_texture_cache[free_slot];
	if (load_texture(mission_index, texture_id, texture_kind, slot, &load_status))
	{
		slot->texture_id = texture_id;
		slot->texture_kind = texture_kind;
		slot->state = 1;
		slot->refs = 1;
		slot->last_used_frame = s_frame;
		slot->fail_count = 0;
		s_tex_loaded++;
		return free_slot;
	}

	if (load_status == 1)
	{
		/* Memoire pleine : le modele sera retente plus tard */
		*transient = 1;
		return -1;
	}

	if (load_status == 2 && prior_fails + 1 < 3)
	{
		/* Import refuse : on garde la trace et on fait retenter le modele */
		slot->texture_id = texture_id;
		slot->texture_kind = texture_kind;
		slot->state = 3;
		slot->refs = 0;
		slot->fail_count = prior_fails + 1;
		*transient = 1;
		return -1;
	}

	slot->texture_id = texture_id;
	slot->texture_kind = texture_kind;
	slot->state = 2;
	slot->refs = 0;
	s_tex_failed++;
	return -1;
}

static void texture_release(int slot_index)
{
	LoadedTexture *t;
	if (slot_index < 0 || (uint32_t)slot_index >= s_texture_cache_count)
		return;
	t = &s_texture_cache[slot_index];
	if (t->state != 1)
		return;
	if (t->refs > 0)
		t->refs--;
	t->last_used_frame = s_frame;
	/* Garde la texture en VRAM en cache : evite le clignotement blanc
	 * si le meme modele ou un modele voisin est recharge peu apres. */
}

static void model_release(Model *model)
{
	uint32_t b;
	if (model->batches)
	{
		for (b = 0; b < model->batch_count; ++b)
			if (model->batches[b].texture_slot >= 0)
				texture_release(model->batches[b].texture_slot);
		free(model->batches);
	}
	if (model->vertices)
		linearFree(model->vertices);
	if (model->indices)
		linearFree(model->indices);
	if (model->loaded && s_models_loaded > 0)
		s_models_loaded--;
	memset(model, 0, sizeof(*model));
}

/* 1 = ok, 0 = echec permanent, -1 = memoire insuffisante (reessayer) */
static int model_load(uint32_t mission_index, uint32_t model_id, Model *out_model)
{
	char path[128];
	uint8_t *data = NULL;
	size_t size = 0;
	uint32_t vertex_count;
	uint32_t index_count;
	uint32_t batch_count;
	size_t batch_offset;
	size_t vertex_offset;
	size_t index_offset;
	uint32_t index;
	float position_origin[3];
	float position_scale[3];
	float uv_origin[2];
	float uv_scale[2];
	float light_uv_origin[2];
	float light_uv_scale[2];
	int result = 0;

	if (out_model->loaded)
		return 1;

	snprintf(path, sizeof(path), "%s/mesh/%s/%05lu.c47m", DATA_ROOT, s_mission_name, (unsigned long)model_id);
	if (!read_file(path, &data, &size) || size < MODEL_HEADER_SIZE
		|| memcmp(data, "C47M", 4) != 0 || read_u16(data + 4) != 1
		|| read_u16(data + 6) != MODEL_HEADER_SIZE)
		goto fail;

	vertex_count = read_u32(data + 8);
	index_count = read_u32(data + 12);
	batch_count = read_u32(data + 16);
	if (vertex_count == 0 || index_count == 0 || batch_count == 0
		|| vertex_count > 65535 || batch_count > 4096)
		goto fail;

	batch_offset = MODEL_HEADER_SIZE;
	vertex_offset = batch_offset + (size_t)batch_count * MODEL_BATCH_SIZE;
	index_offset = vertex_offset + (size_t)vertex_count * MODEL_VERTEX_SIZE;
	if (index_offset > size || (size_t)index_count > (size - index_offset) / 2
		|| index_offset + (size_t)index_count * 2 != size)
		goto fail;

	for (index = 0; index < 3; ++index)
	{
		position_origin[index] = read_f32(data + 24 + index * 4);
		position_scale[index] = read_f32(data + 36 + index * 4);
	}
	for (index = 0; index < 2; ++index)
	{
		uv_origin[index] = read_f32(data + 48 + index * 4);
		uv_scale[index] = read_f32(data + 56 + index * 4);
		light_uv_origin[index] = read_f32(data + 64 + index * 4);
		light_uv_scale[index] = read_f32(data + 72 + index * 4);
	}

	out_model->vertices = linearAlloc((size_t)vertex_count * sizeof(Vertex));
	out_model->indices = linearAlloc((size_t)index_count * sizeof(uint16_t));
	out_model->batches = calloc(batch_count, sizeof(DrawBatch));
	if (!out_model->vertices || !out_model->indices || !out_model->batches)
	{
		result = -1; /* manque de memoire : on reessaiera apres eviction */
		goto fail;
	}

	/* Tous les slots a -1 AVANT toute chose (calloc met 0 = slot valide !) */
	out_model->batch_count = batch_count;
	for (index = 0; index < batch_count; ++index)
		out_model->batches[index].texture_slot = -1;

	for (index = 0; index < vertex_count; ++index)
	{
		const uint8_t *record = data + vertex_offset + index * MODEL_VERTEX_SIZE;
		Vertex *vertex = (Vertex *)out_model->vertices + index;
		uint32_t axis;

		for (axis = 0; axis < 3; ++axis)
		{
			int16_t quantized = (int16_t)read_u16(record + axis * 2);
			vertex->position[axis] = position_origin[axis] + quantized * position_scale[axis];
		}
		for (axis = 0; axis < 2; ++axis)
		{
			uint16_t quantized = read_u16(record + 6 + axis * 2);
			float uv = uv_origin[axis] + quantized * uv_scale[axis];
			uv = sanitize_uv(uv);
#if TEXTURE_FLIP_V
			if (axis == 1)
				uv = 1.0f - uv;
#endif
			vertex->texcoord[axis] = uv;
		}
		for (axis = 0; axis < 4; ++axis)
			vertex->color[axis] = record[14 + axis] / 255.0f;
	}

	for (index = 0; index < index_count; ++index)
	{
		uint16_t vertex_index = read_u16(data + index_offset + index * 2);
		if (vertex_index >= vertex_count)
			goto fail;
		((uint16_t *)out_model->indices)[index] = vertex_index;
	}

	out_model->vertex_count = vertex_count;
	out_model->index_count = index_count;

	for (index = 0; index < batch_count; ++index)
	{
		const uint8_t *record = data + batch_offset + index * MODEL_BATCH_SIZE;
		DrawBatch *batch = &out_model->batches[index];

		batch->texture_id = read_u32(record);
		batch->light_texture_id = read_u32(record + 4);
		batch->texture_kind = 1;
		batch->first_index = read_u32(record + 8);
		batch->index_count = read_u32(record + 12);
		batch->first_vertex = read_u32(record + 16);
		batch->face_flags = read_u32(record + 20);
		batch->texture_slot = -1;

		if (batch->first_index > index_count
			|| batch->index_count > index_count - batch->first_index
			|| batch->index_count == 0 || batch->index_count % 3 != 0)
			goto fail;

		if (batch->texture_id != NO_TEXTURE)
		{
			int transient = 0;
			int slot = get_texture_slot(mission_index, batch->texture_id, 1, &transient);
			if (slot < 0 && !transient)
				slot = get_texture_slot(mission_index, batch->texture_id, 2, &transient);
			if (slot < 0 && !transient && batch->light_texture_id != NO_TEXTURE)
				slot = get_texture_slot(mission_index, batch->light_texture_id, 2, &transient);
			if (slot < 0 && transient)
			{
				result = -1;
				goto fail;
			}
			batch->texture_slot = slot;
		}
		else if (batch->light_texture_id != NO_TEXTURE)
		{
			/*
			 * Face sans texture de base mais avec une lightmap : on affiche la
			 * lightmap (avec ses propres UV) au lieu d'un aplat blanc.
			 */
			int transient = 0;
			int slot = get_texture_slot(mission_index, batch->light_texture_id, 2, &transient);
			if (slot < 0 && !transient)
				slot = get_texture_slot(mission_index, batch->light_texture_id, 1, &transient);
			if (slot < 0 && transient)
			{
				result = -1;
				goto fail;
			}
			batch->texture_slot = slot;
			if (slot >= 0)
			{
				uint32_t vend = vertex_count;
				uint32_t vi;
				if (index + 1 < batch_count)
					vend = read_u32(data + batch_offset
						+ (size_t)(index + 1) * MODEL_BATCH_SIZE + 16);
				if (batch->first_vertex < vend && vend <= vertex_count)
				{
					for (vi = batch->first_vertex; vi < vend; ++vi)
					{
						const uint8_t *vrec = data + vertex_offset + (size_t)vi * MODEL_VERTEX_SIZE;
						Vertex *vtx = (Vertex *)out_model->vertices + vi;
						float lu = light_uv_origin[0] + read_u16(vrec + 10) * light_uv_scale[0];
						float lv = light_uv_origin[1] + read_u16(vrec + 12) * light_uv_scale[1];
						lu = sanitize_uv(lu);
						lv = sanitize_uv(lv);
#if TEXTURE_FLIP_V
						lv = 1.0f - lv;
#endif
						vtx->texcoord[0] = lu;
						vtx->texcoord[1] = lv;
					}
				}
			}
		}
	}

	/* IMPORTANT : le GPU lit la memoire lineaire par DMA, il faut vider le cache CPU */
	GSPGPU_FlushDataCache(out_model->vertices, (size_t)vertex_count * sizeof(Vertex));
	GSPGPU_FlushDataCache(out_model->indices, (size_t)index_count * sizeof(uint16_t));

	out_model->loaded = 1;
	s_models_loaded++;
	free(data);
	return 1;

fail:
	model_release(out_model);
	free(data);
	return result;
}

/* Charge un modele en gerant les etats echec / reessai */
static int model_try_load(uint32_t model_id)
{
	Model *model = &s_models[model_id];
	int r = model_load(s_mission_index, model_id, model);
	if (r == 1)
		return 1;
	if (r == 0)
	{
		model->failed = 1;
		s_models_failed++;
		log_failure("modele %lu: invalide ou absent (ignore) %lu", model_id, 0);
	}
	else
	{
		model->retry_frame = s_frame + 90; /* ~1.5 s plus tard */
		evict_far_models(8); /* libere (et defragmente) la memoire GPU */
	}
	return 0;
}

static int is_secondary_lod(const char *name)
{
	char lower[128];
	size_t len;
	size_t i;
	if (!name || !*name)
		return 0;
	len = strlen(name);
	if (len >= sizeof(lower))
		len = sizeof(lower) - 1;
	for (i = 0; i < len; ++i)
	{
		char c = name[i];
		lower[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
	}
	lower[len] = '\0';

	if (strstr(lower, "superfarlod"))
		return 1;
	if (strstr(lower, "lowestdetail") || strstr(lower, "lowdetail"))
		return 1;
	if (strstr(lower, "loda01") || strstr(lower, "loda02") || strstr(lower, "loda03"))
		return 1;
	if (strstr(lower, "lodb01") || strstr(lower, "lodb02"))
		return 1;
	if (strstr(lower, "lodc01") || strstr(lower, "lodc02"))
		return 1;
	if (strstr(lower, "lodd01") || strstr(lower, "lodd02"))
		return 1;
	if (strstr(lower, "lode01") || strstr(lower, "lode02"))
		return 1;

	return 0;
}

/* Filtre les volumes d'aide, boites d'editeur et marqueurs invisibles
 * qui apparaissent sinon sous forme de murs blancs ou boites flottantes. */
static int is_helper_or_editor_object(const char *name, uint32_t type)
{
	char lower[128];
	size_t len;
	size_t i;
	if (!name || !*name)
		return 0;

	/* Type 28 = InsideBound (toujours un volume d'aide invisible) */
	if (type == 28)
		return 1;

	len = strlen(name);
	if (len >= sizeof(lower))
		len = sizeof(lower) - 1;
	for (i = 0; i < len; ++i)
	{
		char c = name[i];
		lower[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
	}
	lower[len] = '\0';

	/* Les corps de personnages (Characters/.../Ground) NE doivent PAS etre caches ! */
	if (strstr(lower, "characters"))
	{
		/* Mais on cache leurs boites de selection de vetements / collision (les "boules") */
		if (strstr(lower, "clothbox") || strstr(lower, "cloth_selector"))
			return 1;
		return 0;
	}

#if HIDE_HELPER_TYPES
	if (type == 22)
		return 1;
#endif

	/* Boites d'editeur, cameras statiques et triggers (qui faisaient des murs blancs) */
	if (strstr(lower, "clothbox") || strstr(lower, "cloth_selector"))
		return 1;
	if (strstr(lower, "posbox") || strstr(lower, "staticcam") || strstr(lower, "activatebox"))
		return 1;
	if (strstr(lower, "arrow") || strstr(lower, "t1guide"))
		return 1;
	if (strstr(lower, "soundstreet") || strstr(lower, "soundambience") || strstr(lower, "soundenvironment"))
		return 1;
	if (strstr(lower, "insidebound"))
		return 1;
	if (strstr(lower, "actionframe") || strstr(lower, "actiongroup") || strstr(lower, "armorbar"))
		return 1;

	return 0;
}

static void player_update_matrix(SceneObject *obj, float x, float y, float z, float yaw)
{
	float cy = cosf(yaw);
	float sy = sinf(yaw);

	C3D_Mtx *wm = &obj->world_matrix;

	/* Citro3D wm->m stores WZYX: row0=w,z,y,x */
	wm->m[0 * 4 + 3] = cy;
	wm->m[0 * 4 + 2] = 0.0f;
	wm->m[0 * 4 + 1] = sy;
	wm->m[0 * 4 + 0] = x;

	wm->m[1 * 4 + 3] = 0.0f;
	wm->m[1 * 4 + 2] = 1.0f;
	wm->m[1 * 4 + 1] = 0.0f;
	wm->m[1 * 4 + 0] = y;

	wm->m[2 * 4 + 3] = -sy;
	wm->m[2 * 4 + 2] = 0.0f;
	wm->m[2 * 4 + 1] = cy;
	wm->m[2 * 4 + 0] = z;

	wm->m[3 * 4 + 3] = 0.0f;
	wm->m[3 * 4 + 2] = 0.0f;
	wm->m[3 * 4 + 1] = 0.0f;
	wm->m[3 * 4 + 0] = 1.0f;

	obj->position[0] = x;
	obj->position[1] = y;
	obj->position[2] = z;
	obj->center[0] = x;
	obj->center[1] = y + 90.0f;
	obj->center[2] = z;
	obj->radius = 120.0f;
}

/* Collision horizontale : ignore ce qui est franchissable (marche) ou au-dessus de la tete */
static int check_player_collision(float x, float y, float z, float radius)
{
	uint32_t i;
	for (i = 0; i < s_scene_object_count; ++i)
	{
		const SceneObject *obj = &s_scene_objects[i];
		if (obj->model_id == s_player.model_id || obj->model_id == NO_MODEL)
			continue;

		/* Franchissable : sommet de l'objet sous la hauteur de marche */
		if (obj->bounds[4] <= y + STEP_UP)
			continue;
		/* Au-dessus de la tete de 47 */
		if (obj->bounds[1] >= y + 170.0f)
			continue;

		if (x + radius >= obj->bounds[0] && x - radius <= obj->bounds[3] &&
			z + radius >= obj->bounds[2] && z - radius <= obj->bounds[5])
		{
			return 1;
		}
	}
	return 0;
}

/* Hauteur du sol sous (x,z) parmi les objets dont le sommet est <= y + STEP_UP */
static int ground_height(float x, float y, float z, float *out_height)
{
	uint32_t i;
	int found = 0;
	float best = -1.0e9f;

	for (i = 0; i < s_scene_object_count; ++i)
	{
		const SceneObject *obj = &s_scene_objects[i];
		float top;
		if (obj->model_id == s_player.model_id || obj->model_id == NO_MODEL)
			continue;
		top = obj->bounds[4];
		if (top > y + STEP_UP || top < best)
			continue;
		if (x < obj->bounds[0] || x > obj->bounds[3]
			|| z < obj->bounds[2] || z > obj->bounds[5])
			continue;
		best = top;
		found = 1;
	}
	if (found)
		*out_height = best;
	return found;
}

static void player_update_vertical(float dt)
{
	float ground;
	if (!ground_height(s_player.position[0], s_player.position[1],
		s_player.position[2], &ground))
	{
		s_player.vy = 0.0f;
		return;
	}

	if (s_player.position[1] > ground + 0.5f)
	{
		/* Chute libre */
		s_player.vy -= GRAVITY * dt;
		s_player.position[1] += s_player.vy * dt;
		if (s_player.position[1] <= ground)
		{
			s_player.position[1] = ground;
			s_player.vy = 0.0f;
		}
	}
	else
	{
		s_player.vy = 0.0f;
		s_player.position[1] = ground;
	}
}

/*
 * Charge jusqu'a max_loads modeles, les plus proches du point de reference d'abord.
 */
static void stream_models(int max_loads, float radius)
{
	int n;
	float radius_sq = radius * radius;

	for (n = 0; n < max_loads; ++n)
	{
		uint32_t i;
		uint32_t best_model = NO_MODEL;
		float best_dist = radius_sq;

		for (i = 0; i < s_scene_object_count; ++i)
		{
			const SceneObject *obj = &s_scene_objects[i];
			uint32_t mid = obj->model_id;
			float dx, dy, dz, d;
			const Model *m;

			if (mid >= MAX_MODELS)
				continue;
			m = &s_models[mid];
			if (m->loaded || m->failed || s_frame < m->retry_frame)
				continue;

			dx = obj->center[0] - s_camera.position[0];
			dy = obj->center[1] - s_camera.position[1];
			dz = obj->center[2] - s_camera.position[2];
			d = dx * dx + dy * dy + dz * dz;
			if (d <= best_dist)
			{
				best_dist = d;
				best_model = mid;
			}
		}

		if (best_model == NO_MODEL)
			break;
		model_try_load(best_model);
	}
}

/* Libere les modeles qui ne sont plus utiles (loin de la camera) quand la memoire manque */
static void evict_far_models(int max_unloads)
{
	static uint8_t needed[MAX_MODELS];
	uint32_t i;
	float keep = s_view_dist * 1.25f;
	float keep_sq = keep * keep;
	int freed = 0;

	memset(needed, 0, sizeof(needed));
	for (i = 0; i < s_scene_object_count; ++i)
	{
		const SceneObject *obj = &s_scene_objects[i];
		float dx, dy, dz;
		if (obj->model_id >= MAX_MODELS)
			continue;
		dx = obj->center[0] - s_camera.position[0];
		dy = obj->center[1] - s_camera.position[1];
		dz = obj->center[2] - s_camera.position[2];
		if (dx * dx + dy * dy + dz * dz <= keep_sq)
			needed[obj->model_id] = 1;
	}

	for (i = 0; i < MAX_MODELS && freed < max_unloads; ++i)
	{
		if (s_models[i].loaded && !needed[i] && i != s_player.model_id)
		{
			model_release(&s_models[i]);
			freed++;
		}
	}
}

static int scene_load(void)
{
	char path[128];
	uint8_t *data = NULL;
	size_t size = 0;
	uint32_t object_count;
	uint32_t object_offset;
	uint32_t string_offset;
	uint32_t string_size;
	uint32_t index;

	if (!find_mission_index(&s_mission_index))
	{
		snprintf(s_error, sizeof(s_error), "Mission %s introuvable dans index.bin", s_mission_name);
		log_message(s_error);
		return 0;
	}

	snprintf(path, sizeof(path), "%s/scene/%s.c47s", DATA_ROOT, s_mission_name);
	if (!read_file(path, &data, &size) || size < 56
		|| memcmp(data, "C47S", 4) != 0 || read_u16(data + 4) != 1)
	{
		snprintf(s_error, sizeof(s_error), "Scene %s.c47s introuvable ou invalide", s_mission_name);
		log_message(s_error);
		free(data);
		return 0;
	}

	object_count = read_u32(data + 8);
	object_offset = read_u32(data + 32);
	string_offset = read_u32(data + 44);
	string_size = read_u32(data + 48);

	/* Verification des bornes pour ne jamais lire hors du fichier */
	if (object_offset > size || object_count > (size - object_offset) / 120
		|| string_offset > size || string_size > size - string_offset)
	{
		snprintf(s_error, sizeof(s_error), "Scene %s.c47s corrompue", s_mission_name);
		log_message(s_error);
		free(data);
		return 0;
	}

	s_scene_object_count = 0;
	s_player.scene_obj_index = -1;
	uint32_t detected_player_model = NO_MODEL;

	for (index = 0; index < object_count && s_scene_object_count < MAX_SCENE_OBJECTS; ++index)
	{
		const uint8_t *record = data + object_offset + index * 120;
		uint32_t model_id = read_u32(record + 12);
		uint32_t name_offset = read_u32(record + 20);
		uint32_t scene_flags = read_u32(record + 24);
		char obj_name[128];
		float matrix[16];
		SceneObject *obj;
		uint32_t k;
		int is_hero_candidate = 0;

		if (model_id == NO_MODEL || model_id >= MAX_MODELS)
			continue;
		if (scene_flags & 1)
			continue;

		if (read_string(data + string_offset, string_size,
			name_offset, obj_name, sizeof(obj_name)))
		{
			char lower_hero[128];
			size_t hlen = strlen(obj_name);
			size_t hi;
			if (hlen >= sizeof(lower_hero))
				hlen = sizeof(lower_hero) - 1;
			for (hi = 0; hi < hlen; ++hi)
				lower_hero[hi] = (obj_name[hi] >= 'A' && obj_name[hi] <= 'Z') ? (char)(obj_name[hi] + 32) : obj_name[hi];
			lower_hero[hlen] = '\0';

			if (is_secondary_lod(obj_name))
				continue;
			if (is_helper_or_editor_object(obj_name, read_u32(record + 4)))
				continue;

			if (strstr(lower_hero, "hero/ground") || strstr(lower_hero, "characters/hero"))
				is_hero_candidate = 1;
		}
		else
		{
			/* Pas de nom : verifier le type brut */
			if (read_u32(record + 4) == 28)
				continue;
		}

		for (k = 0; k < 16; ++k)
			matrix[k] = read_f32(record + 32 + k * 4);

		if (is_hero_candidate || (s_player.scene_obj_index < 0 && model_id == 1514))
		{
			s_player.scene_obj_index = (int)s_scene_object_count;
			detected_player_model = model_id;
		}

		obj = &s_scene_objects[s_scene_object_count++];
		obj->id = read_u32(record);
		obj->type = read_u32(record + 4);
		obj->model_id = model_id;
		obj->position[0] = matrix[12];
		obj->position[1] = matrix[13];
		obj->position[2] = matrix[14];

		/*
		 * Matrice Hitman : row-major, convention point x matrice.
		 * On transpose pour la convention colonne-vecteur du PICA200.
		 * C3D_FVec stocke en WZYX.
		 */
		{
			C3D_Mtx *wm = &obj->world_matrix;
			uint32_t row_i, col_i;
			for (row_i = 0; row_i < 4; ++row_i)
			{
				float vals[4];
				for (col_i = 0; col_i < 4; ++col_i)
					vals[col_i] = matrix[col_i * 4 + row_i];
				wm->m[row_i * 4 + 3] = vals[0]; /* x */
				wm->m[row_i * 4 + 2] = vals[1]; /* y */
				wm->m[row_i * 4 + 1] = vals[2]; /* z */
				wm->m[row_i * 4 + 0] = vals[3]; /* w */
			}
		}

		for (k = 0; k < 6; ++k)
			obj->bounds[k] = read_f32(record + 96 + k * 4);

		obj->center[0] = (obj->bounds[0] + obj->bounds[3]) * 0.5f;
		obj->center[1] = (obj->bounds[1] + obj->bounds[4]) * 0.5f;
		obj->center[2] = (obj->bounds[2] + obj->bounds[5]) * 0.5f;

		{
			float hx = (obj->bounds[3] - obj->bounds[0]) * 0.5f;
			float hy = (obj->bounds[4] - obj->bounds[1]) * 0.5f;
			float hz = (obj->bounds[5] - obj->bounds[2]) * 0.5f;
			obj->radius = sqrtf(hx * hx + hy * hy + hz * hz);
			if (obj->radius < 100.0f)
				obj->radius = 100.0f;
		}
	}

	free(data);

	/*
	 * Position de depart :
	 * - C1_1 : position connue
	 * - autre mission avec 47 dans la scene : sa position
	 * - sinon : centre de la carte, en camera libre
	 */
	s_spawn_fallback = 0;
	s_player.position[0] = 0.0f;
	s_player.position[1] = 0.0f;
	s_player.position[2] = 0.0f;
	if (strcmp(s_mission_name, DEFAULT_MISSION) == 0)
	{
		s_player.position[0] = 1217.88f;
		s_player.position[1] = 0.0f;
		s_player.position[2] = -4939.37f;
	}
	else if (s_player.scene_obj_index >= 0)
	{
		const SceneObject *spawn = &s_scene_objects[s_player.scene_obj_index];
		s_player.position[0] = spawn->position[0];
		s_player.position[1] = spawn->position[1];
		s_player.position[2] = spawn->position[2];
	}
	else
	{
		float sx = 0.0f, sy = 0.0f, sz = 0.0f;
		uint32_t n = 0;
		uint32_t si;
		for (si = 0; si < s_scene_object_count; ++si)
		{
			const SceneObject *so = &s_scene_objects[si];
			if (so->model_id == detected_player_model && detected_player_model != NO_MODEL)
				continue;
			sx += so->center[0];
			sy += so->center[1];
			sz += so->center[2];
			++n;
		}
		if (n > 0)
		{
			s_player.position[0] = sx / (float)n;
			s_player.position[1] = sy / (float)n + 1000.0f;
			s_player.position[2] = sz / (float)n;
		}
		s_spawn_fallback = 1;
	}
	s_player.yaw = 0.0f;
	s_player.vy = 0.0f;
	if (detected_player_model != NO_MODEL)
		s_player.model_id = detected_player_model;
	else if (s_player.scene_obj_index >= 0)
		s_player.model_id = s_scene_objects[s_player.scene_obj_index].model_id;
	else
		s_player.model_id = 1514;

	if (s_player.scene_obj_index < 0 && s_scene_object_count < MAX_SCENE_OBJECTS)
	{
		SceneObject *p_obj;
		s_player.scene_obj_index = (int)s_scene_object_count++;
		p_obj = &s_scene_objects[s_player.scene_obj_index];
		memset(p_obj, 0, sizeof(*p_obj));
		p_obj->id = 0x4747;
		p_obj->model_id = s_player.model_id;
	}

	/* Poser 47 sur le sol (si on en trouve un) */
	if (!s_spawn_fallback)
	{
		float g;
		float start_y = s_player.position[1] + 200.0f;
		if (ground_height(s_player.position[0], start_y,
			s_player.position[2], &g) && g < start_y)
			s_player.position[1] = g;
	}
	s_camera_mode = s_spawn_fallback ? CAMERA_MODE_FREE : CAMERA_MODE_THIRD_PERSON;

	if (s_player.scene_obj_index >= 0)
	{
		player_update_matrix(&s_scene_objects[s_player.scene_obj_index],
			s_player.position[0], s_player.position[1], s_player.position[2], s_player.yaw);
	}

	/* Position camera 3eme personne derriere 47 */
	s_cam_orbit_yaw = s_player.yaw;
	s_cam_orbit_pitch = 0.20f;
	s_cam_orbit_dist = 360.0f;
	s_cam_target_y = s_player.position[1];

	s_camera.position[0] = s_player.position[0] - sinf(s_cam_orbit_yaw) * cosf(s_cam_orbit_pitch) * s_cam_orbit_dist;
	s_camera.position[1] = s_player.position[1] + 130.0f + sinf(s_cam_orbit_pitch) * s_cam_orbit_dist;
	s_camera.position[2] = s_player.position[2] - cosf(s_cam_orbit_yaw) * cosf(s_cam_orbit_pitch) * s_cam_orbit_dist;
	s_camera.yaw = s_cam_orbit_yaw;
	s_camera.pitch = -s_cam_orbit_pitch;

	/* 47 d'abord */
	if (s_player.model_id != NO_MODEL)
		model_try_load(s_player.model_id);

	return s_scene_object_count > 0;
}

/* Decharge toute la mission courante (modeles, textures, objets) */
static void scene_unload(void)
{
	uint32_t i;
	for (i = 0; i < MAX_MODELS; ++i)
	{
		if (s_models[i].loaded)
			model_release(&s_models[i]);
	}
	for (i = 0; i < s_texture_cache_count; ++i)
	{
		if (s_texture_cache[i].state == 1)
			C3D_TexDelete(&s_texture_cache[i].texture);
		s_texture_cache[i].state = 0;
	}
	s_texture_cache_count = 0;
	if (s_tex_index_data)
	{
		free(s_tex_index_data);
		s_tex_index_data = NULL;
		s_tex_index_size = 0;
	}
	/* Remet a zero les drapeaux (echecs, reessais) pour la prochaine mission */
	memset(s_models, 0, sizeof(s_models));
	s_tex_loaded = 0;
	s_tex_failed = 0;
	s_models_loaded = 0;
	s_models_failed = 0;
	s_scene_object_count = 0;
	s_player.scene_obj_index = -1;
}

static void scene_free_all(void)
{
	scene_unload();
	if (s_white_texture_initialized)
	{
		C3D_TexDelete(&s_white_texture);
		s_white_texture_initialized = 0;
	}
}

/* Le brouillard suit la distance d'affichage choisie */
static void fog_rebuild(void)
{
	/* Plan lointain colle a la distance d'affichage : meilleure precision de profondeur */
	float far_plane = s_view_dist + 4000.0f;
	Mtx_PerspTilt(&s_projection, C3D_AngleFromDegrees(60.0f),
		C3D_AspectRatioTop, NEAR_PLANE, far_plane, false);
	FogLut_Exp(&s_fog_lut, 1.15f / s_view_dist, 1.6f, NEAR_PLANE, far_plane);
	if (s_fog_enabled)
		C3D_FogLutBind(&s_fog_lut);
}

static void fog_apply(void)
{
	if (s_fog_enabled)
	{
		C3D_FogGasMode(GPU_FOG, GPU_PLAIN_DENSITY, false);
		C3D_FogColor(0x181820);
		C3D_FogLutBind(&s_fog_lut);
	}
	else
	{
		C3D_FogGasMode(GPU_NO_FOG, GPU_PLAIN_DENSITY, false);
	}
}

static int scene_init(void)
{
	s_shader_binary = DVLB_ParseFile((u32 *)vshader_shbin, vshader_shbin_size);
	if (!s_shader_binary)
		return 0;
	if (R_FAILED(shaderProgramInit(&s_shader_program)))
		return 0;
	shaderProgramSetVsh(&s_shader_program, &s_shader_binary->DVLE[0]);
	C3D_BindProgram(&s_shader_program);

	s_projection_location = shaderInstanceGetUniformLocation(
		s_shader_program.vertexShader, "projection");
	s_model_view_location = shaderInstanceGetUniformLocation(
		s_shader_program.vertexShader, "modelView");

	AttrInfo_Init(C3D_GetAttrInfo());
	AttrInfo_AddLoader(C3D_GetAttrInfo(), 0, GPU_FLOAT, 3);
	AttrInfo_AddLoader(C3D_GetAttrInfo(), 1, GPU_FLOAT, 2);
	AttrInfo_AddLoader(C3D_GetAttrInfo(), 2, GPU_FLOAT, 4);

	Mtx_PerspTilt(&s_projection, C3D_AngleFromDegrees(60.0f),
		C3D_AspectRatioTop, NEAR_PLANE, FAR_PLANE, false);

	/*
	 * Brouillard : les unites sont des centimetres. L'ancienne densite (0.04)
	 * rendait TOUT opaque au-dela de ~60 unites, donc la scene entiere etait
	 * noyee dans la couleur du brouillard. Ici : visible vers 60 m, dense vers 160 m.
	 */
	fog_rebuild();
	fog_apply();

	/* Texture blanche pour surfaces sans texture */
	if (!s_white_texture_initialized)
	{
		if (C3D_TexInit(&s_white_texture, 8, 8, GPU_RGBA8))
		{
			/* Teinte neutre gris/ombre au lieu d'un blanc aveuglant 0xFF */
			memset(s_white_texture.data, 0x70, 8 * 8 * 4);
			C3D_TexFlush(&s_white_texture); /* indispensable : sinon le GPU lit du vide */
			C3D_TexSetFilter(&s_white_texture, GPU_LINEAR, GPU_LINEAR);
			C3D_TexSetWrap(&s_white_texture, GPU_REPEAT, GPU_REPEAT);
			s_white_texture_initialized = 1;
		}
	}

	C3D_TexEnv *environment = C3D_GetTexEnv(0);
	C3D_TexEnvInit(environment);
	C3D_TexEnvSrc(environment, C3D_Both,
		GPU_TEXTURE0, GPU_PRIMARY_COLOR, 0);
	C3D_TexEnvFunc(environment, C3D_Both, GPU_MODULATE);

	C3D_DepthTest(true, GPU_GREATER, GPU_WRITE_ALL);
	C3D_CullFace(GPU_CULL_NONE);
	C3D_AlphaTest(true, GPU_GREATER, 32);

	return 1;
}

static int compare_visible_objects(const void *a, const void *b)
{
	const VisibleObject *oa = (const VisibleObject *)a;
	const VisibleObject *ob = (const VisibleObject *)b;
	if (oa->dist_sq < ob->dist_sq) return -1;
	if (oa->dist_sq > ob->dist_sq) return 1;
	return 0;
}

static void scene_draw(void)
{
	uint32_t index;
	uint32_t v;
	uint32_t visible_count = 0;
	C3D_Mtx view_matrix;
	const Model *last_model = NULL;

	/* Demi-angles du champ de vision (fov vertical 60 deg, ecran 400x240) */
	const float tan_v = 0.57735f * 1.08f;
	const float tan_h = 0.57735f * C3D_AspectRatioTop * 1.08f;
	const float norm_v = sqrtf(1.0f + tan_v * tan_v);
	const float norm_h = sqrtf(1.0f + tan_h * tan_h);

	/* Repere camera dans le monde */
	float cos_pitch = cosf(s_camera.pitch);
	float forward_x = sinf(s_camera.yaw) * cos_pitch;
	float forward_y = sinf(s_camera.pitch);
	float forward_z = cosf(s_camera.yaw) * cos_pitch;
	float right_x = cosf(s_camera.yaw);
	float right_z = -sinf(s_camera.yaw);
	/* up = right x forward (right_y = 0) */
	float up_x = -right_z * forward_y;
	float up_y = right_z * forward_x - right_x * forward_z;
	float up_z = right_x * forward_y;

	C3D_FVec cam_pos = FVec3_New(s_camera.position[0], s_camera.position[1], s_camera.position[2]);
	C3D_FVec cam_target = FVec3_New(
		s_camera.position[0] + forward_x,
		s_camera.position[1] + forward_y,
		s_camera.position[2] + forward_z);
	C3D_FVec cam_up = FVec3_New(0.0f, 1.0f, 0.0f);

	s_drawn_objects = 0;
	s_drawn_triangles = 0;
	s_draw_calls = 0;

	Mtx_LookAt(&view_matrix, cam_pos, cam_target, cam_up, false);
	C3D_FVUnifMtx4x4(GPU_VERTEX_SHADER, s_projection_location, &s_projection);

	/* Selection des objets visibles : distance + frustum (sphere englobante) */
	for (index = 0; index < s_scene_object_count; ++index)
	{
		const SceneObject *obj = &s_scene_objects[index];
		float dx, dy, dz, dist_sq, xc, yc, zc, r, max_dist;
		Model *model;

		if (obj->model_id >= MAX_MODELS)
			continue;

		model = &s_models[obj->model_id];
		if (!model->loaded || !model->vertices || model->batch_count == 0)
			continue;

		dx = obj->center[0] - s_camera.position[0];
		dy = obj->center[1] - s_camera.position[1];
		dz = obj->center[2] - s_camera.position[2];
		dist_sq = dx * dx + dy * dy + dz * dz;
		r = obj->radius + 50.0f;

		max_dist = s_view_dist + r;
		if (dist_sq > max_dist * max_dist)
			continue;

		zc = dx * forward_x + dy * forward_y + dz * forward_z;
		if (zc < -r)
			continue;

		/* Cotes gauche / droite */
		xc = dx * right_x + dz * right_z;
		if (fabsf(xc) - zc * tan_h > r * norm_h)
			continue;

		/* Haut / bas */
		yc = dx * up_x + dy * up_y + dz * up_z;
		if (fabsf(yc) - zc * tan_v > r * norm_v)
			continue;

		s_visible_objects[visible_count].obj_index = index;
		s_visible_objects[visible_count].dist_sq = dist_sq;
		visible_count++;
	}

	/* Tri front-to-back */
	if (visible_count > 1)
		qsort(s_visible_objects, visible_count, sizeof(VisibleObject), compare_visible_objects);

	for (v = 0; v < visible_count; ++v)
	{
		const SceneObject *obj;
		Model *model;
		C3D_Mtx model_view;
		uint32_t b;

		index = s_visible_objects[v].obj_index;
		obj = &s_scene_objects[index];
		model = &s_models[obj->model_id];

		if (s_draw_calls + model->batch_count >= MAX_FRAME_DRAW_CALLS)
			break;

		Mtx_Multiply(&model_view, &view_matrix, &obj->world_matrix);
		C3D_FVUnifMtx4x4(GPU_VERTEX_SHADER, s_model_view_location, &model_view);

		if (last_model != model)
		{
			BufInfo_Init(C3D_GetBufInfo());
			BufInfo_Add(C3D_GetBufInfo(), model->vertices, sizeof(Vertex), 3, 0x210);
			last_model = model;
		}

		for (b = 0; b < model->batch_count; ++b)
		{
			const DrawBatch *batch = &model->batches[b];

			/* Faces sans texture ni lightmap = volumes d'aide invisibles : on ne les dessine pas */
			if (batch->texture_id == NO_TEXTURE && batch->light_texture_id == NO_TEXTURE)
				continue;

			if (batch->texture_slot >= 0
				&& (uint32_t)batch->texture_slot < s_texture_cache_count
				&& s_texture_cache[batch->texture_slot].state == 1)
			{
				C3D_TexBind(0, &s_texture_cache[batch->texture_slot].texture);
			}
			else if (s_white_texture_initialized)
			{
				C3D_TexBind(0, &s_white_texture);
			}

			C3D_DrawElements(GPU_TRIANGLES, batch->index_count,
				C3D_UNSIGNED_SHORT,
				(uint16_t *)model->indices + batch->first_index);

			s_drawn_triangles += batch->index_count / 3;
			s_draw_calls++;
		}
		s_drawn_objects++;
	}
}

static void scene_exit(void)
{
	scene_free_all();
	if (s_shader_program.vertexShader)
		shaderProgramFree(&s_shader_program);
	if (s_shader_binary)
		DVLB_Free(s_shader_binary);
	if (s_top_target)
		C3D_RenderTargetDelete(s_top_target);
}

static void log_init(void)
{
	mkdir("sdmc:/3ds", 0777);
	mkdir("sdmc:/3ds/hitman47", 0777);
	s_log = fopen("sdmc:/3ds/hitman47/log.txt", "a");
	log_message("Hitman 47 New 3DS application started");
}

static void render_blank_frame(void)
{
	if (s_citro3d_initialized && s_top_target
		&& C3D_FrameBegin(C3D_FRAME_SYNCDRAW))
	{
		C3D_RenderTargetClear(s_top_target, C3D_CLEAR_ALL, CLEAR_COLOR, 0);
		C3D_FrameDrawOn(s_top_target);
		C3D_FrameEnd(0);
	}
}

/* Dessine une barre de chargement sur l'ecran du bas (console).
 * fraction = 0.0 a 1.0, label = texte a afficher sous la barre. */
static void render_loading_bar(float fraction, const char *label)
{
	/* Largeur en caracteres (l'ecran du bas fait 40 colonnes en console 3DS) */
	const int BAR_WIDTH = 36;
	char bar[40];
	int filled, i;

	if (fraction < 0.0f) fraction = 0.0f;
	if (fraction > 1.0f) fraction = 1.0f;
	filled = (int)(fraction * BAR_WIDTH);

	bar[0] = '[';
	for (i = 1; i <= BAR_WIDTH; ++i)
		bar[i] = (i <= filled) ? '#' : '-';
	bar[BAR_WIDTH + 1] = ']';
	bar[BAR_WIDTH + 2] = '\0';

	/* Deplace le curseur en haut, efface l'ecran, puis affiche */
	printf("\x1b[1;1H");
	printf("=== Hitman 47 - Chargement ===\x1b[K\n\n");
	printf("  Mission : %s\x1b[K\n\n", s_mission_name);
	printf("  %s  %3d%%\x1b[K\n\n", bar, (int)(fraction * 100.0f));
	printf("  %-.36s\x1b[K\n", label ? label : "");
}

/* Compte combien de modeles de la scene ne sont ni charges ni en echec. */
static int count_models_to_load(void)
{
	int count = 0;
	uint32_t i;
	for (i = 0; i < s_scene_object_count; ++i)
	{
		uint32_t mid = s_scene_objects[i].model_id;
		if (mid < MAX_MODELS && !s_models[mid].loaded && !s_models[mid].failed)
			count++;
	}
	return count;
}

/* Charge la mission choisie dans le menu. Retourne 1 si OK. */
static int start_mission(int index)
{
	char line[96];
	int total_models;
	int pass;

	snprintf(s_mission_name, sizeof(s_mission_name), "%s", s_mission_names[index]);
	s_error[0] = '\0';
	s_fail_logged = 0;

	snprintf(line, sizeof(line), "Chargement de la mission %s", s_mission_name);
	log_message(line);

	/* --- Phase 1 : lecture de la scene (rapide) --- */
	printf("\x1b[2J");
	render_loading_bar(0.0f, "Lecture de la scene...");
	render_blank_frame();

	if (!scene_load())
	{
		if (!s_error[0])
			snprintf(s_error, sizeof(s_error), "Impossible de charger %s", s_mission_name);
		scene_unload();
		return 0;
	}

	/* --- Phase 2 : pre-chargement de tous les modeles proches --- */
	total_models = (int)s_scene_object_count;
	if (total_models < 1)
		total_models = 1;

	for (pass = 0; pass < 512; ++pass)
	{
		int remaining;
		int loaded_now;
		float progress;
		char status[64];

		/* Charge un lot de modeles */
		stream_models(8, s_view_dist * 2.0f);

		remaining = count_models_to_load();
		loaded_now = (int)s_models_loaded;
		progress = 0.05f + 0.95f * (float)loaded_now / (float)(loaded_now + remaining + 1);

		snprintf(status, sizeof(status), "%d modeles charges, %d restants",
			loaded_now, remaining);
		render_loading_bar(progress, status);

		/* On affiche la barre sur l'ecran du bas en rendant une frame vide sur le haut */
		if (s_citro3d_initialized && s_top_target && C3D_FrameBegin(C3D_FRAME_SYNCDRAW))
		{
			C3D_RenderTargetClear(s_top_target, C3D_CLEAR_ALL, CLEAR_COLOR, 0);
			C3D_FrameDrawOn(s_top_target);
			C3D_FrameEnd(0);
		}

		/* Arret quand plus rien a charger ou si la memoire est trop pleine */
		if (remaining == 0)
			break;
		if (linearSpaceFree() < LOW_LINEAR_NOSTREAM)
			break;
	}

	render_loading_bar(1.0f, "Pret !");
	if (s_citro3d_initialized && s_top_target && C3D_FrameBegin(C3D_FRAME_SYNCDRAW))
	{
		C3D_RenderTargetClear(s_top_target, C3D_CLEAR_ALL, CLEAR_COLOR, 0);
		C3D_FrameDrawOn(s_top_target);
		C3D_FrameEnd(0);
	}

	return 1;
}

static void update_play(u32 keys, u32 keys_down, circlePosition cpad,
	circlePosition cstick, float delta_seconds)
{
	float rot_speed = 2.0f;
	float speed = (keys & KEY_B) ? 3000.0f : 1200.0f;
	float prev_dist = s_view_dist;

	/* A : brouillard on/off */
	if (keys_down & KEY_A)
	{
		s_fog_enabled = !s_fog_enabled;
		fog_apply();
	}

	/* L / R : distance d'affichage -/+ (le brouillard suit) */
	if (keys & KEY_R)
		s_view_dist += 6000.0f * delta_seconds;
	if (keys & KEY_L)
		s_view_dist -= 6000.0f * delta_seconds;
	if (s_view_dist > MAX_VIEW_LIMIT) s_view_dist = MAX_VIEW_LIMIT;
	if (s_view_dist < MIN_VIEW_DIST) s_view_dist = MIN_VIEW_DIST;
	if (s_view_dist != prev_dist)
		fog_rebuild();

	/* SELECT : Agent 47 3eme personne <-> Camera Libre */
	if (keys_down & KEY_SELECT)
	{
		if (s_camera_mode == CAMERA_MODE_THIRD_PERSON)
		{
			s_camera_mode = CAMERA_MODE_FREE;
		}
		else
		{
			s_camera_mode = CAMERA_MODE_THIRD_PERSON;
			s_cam_orbit_yaw = s_player.yaw;
			s_cam_orbit_pitch = 0.20f;
		}
	}

	if (s_camera_mode == CAMERA_MODE_THIRD_PERSON)
	{
		float fwd_x, fwd_z, rgt_x, rgt_z, input_x, input_z;
		float cam_cos_p, cam_sin_p, cam_dir_x, cam_dir_y, cam_dir_z;
		float target_x, target_y, target_z, blend;

		if (abs(cstick.dx) > 15)
			s_cam_orbit_yaw -= ((float)cstick.dx / 156.0f) * rot_speed * delta_seconds;
		if (abs(cstick.dy) > 15)
			s_cam_orbit_pitch += ((float)cstick.dy / 156.0f) * rot_speed * delta_seconds;

		if (keys & KEY_DLEFT)
			s_cam_orbit_yaw -= rot_speed * delta_seconds;
		if (keys & KEY_DRIGHT)
			s_cam_orbit_yaw += rot_speed * delta_seconds;
		if (keys & KEY_DUP)
			s_cam_orbit_pitch += rot_speed * delta_seconds;
		if (keys & KEY_DDOWN)
			s_cam_orbit_pitch -= rot_speed * delta_seconds;

		if (s_cam_orbit_pitch > 0.85f) s_cam_orbit_pitch = 0.85f;
		if (s_cam_orbit_pitch < -0.30f) s_cam_orbit_pitch = -0.30f;

		fwd_x = sinf(s_cam_orbit_yaw);
		fwd_z = cosf(s_cam_orbit_yaw);
		rgt_x = cosf(s_cam_orbit_yaw);
		rgt_z = -sinf(s_cam_orbit_yaw);

		input_x = 0.0f;
		input_z = 0.0f;
		if (abs(cpad.dy) > 15)
			input_z = ((float)cpad.dy / 156.0f);
		if (abs(cpad.dx) > 15)
			input_x = ((float)cpad.dx / 156.0f);

		if (fabsf(input_x) > 0.01f || fabsf(input_z) > 0.01f)
		{
			float move_dir_x = fwd_x * input_z + rgt_x * input_x;
			float move_dir_z = fwd_z * input_z + rgt_z * input_x;
			float move_len = sqrtf(move_dir_x * move_dir_x + move_dir_z * move_dir_z);
			if (move_len > 0.01f)
			{
				float player_speed, amount, move_x, move_z, tx, tz;

				move_dir_x /= move_len;
				move_dir_z /= move_len;

				{
					float target_yaw = atan2f(move_dir_x, move_dir_z);
					float diff = target_yaw - s_player.yaw;
					while (diff > 3.14159265f) diff -= 6.2831853f;
					while (diff < -3.14159265f) diff += 6.2831853f;
					s_player.yaw += diff * fminf(1.0f, 14.0f * delta_seconds);
				}

				player_speed = (keys & KEY_B) ? 750.0f : 350.0f;
				amount = move_len > 1.0f ? 1.0f : move_len;
				move_x = move_dir_x * player_speed * amount * delta_seconds;
				move_z = move_dir_z * player_speed * amount * delta_seconds;

				tx = s_player.position[0] + move_x;
				tz = s_player.position[2] + move_z;
				if (!check_player_collision(tx, s_player.position[1], s_player.position[2], 35.0f))
					s_player.position[0] = tx;
				if (!check_player_collision(s_player.position[0], s_player.position[1], tz, 35.0f))
					s_player.position[2] = tz;
			}
		}

		player_update_vertical(delta_seconds);

		if (s_player.scene_obj_index >= 0 && (uint32_t)s_player.scene_obj_index < s_scene_object_count)
		{
			player_update_matrix(&s_scene_objects[s_player.scene_obj_index],
				s_player.position[0], s_player.position[1], s_player.position[2], s_player.yaw);
		}

		blend = fminf(1.0f, 8.0f * delta_seconds);
		s_cam_target_y += (s_player.position[1] - s_cam_target_y) * blend;

		cam_cos_p = cosf(s_cam_orbit_pitch);
		cam_sin_p = sinf(s_cam_orbit_pitch);
		cam_dir_x = sinf(s_cam_orbit_yaw) * cam_cos_p;
		cam_dir_y = cam_sin_p;
		cam_dir_z = cosf(s_cam_orbit_yaw) * cam_cos_p;

		target_x = s_player.position[0];
		target_y = s_cam_target_y + 130.0f;
		target_z = s_player.position[2];

		s_camera.position[0] = target_x - cam_dir_x * s_cam_orbit_dist;
		s_camera.position[1] = target_y + cam_dir_y * s_cam_orbit_dist;
		s_camera.position[2] = target_z - cam_dir_z * s_cam_orbit_dist;
		s_camera.yaw = s_cam_orbit_yaw;
		s_camera.pitch = -s_cam_orbit_pitch;
	}
	else
	{
		float forward_x, forward_z, right_x, right_z;

		if (abs(cstick.dx) > 15)
			s_camera.yaw -= ((float)cstick.dx / 156.0f) * rot_speed * delta_seconds;
		if (abs(cstick.dy) > 15)
			s_camera.pitch += ((float)cstick.dy / 156.0f) * rot_speed * delta_seconds;

		if (keys & KEY_DLEFT)
			s_camera.yaw -= rot_speed * delta_seconds;
		if (keys & KEY_DRIGHT)
			s_camera.yaw += rot_speed * delta_seconds;
		if (keys & KEY_DUP)
			s_camera.pitch += rot_speed * delta_seconds;
		if (keys & KEY_DDOWN)
			s_camera.pitch -= rot_speed * delta_seconds;

		if (s_camera.pitch > 1.4f) s_camera.pitch = 1.4f;
		if (s_camera.pitch < -1.4f) s_camera.pitch = -1.4f;

		forward_x = sinf(s_camera.yaw);
		forward_z = cosf(s_camera.yaw);
		right_x = cosf(s_camera.yaw);
		right_z = -sinf(s_camera.yaw);

		if (abs(cpad.dy) > 15)
		{
			float move = ((float)cpad.dy / 156.0f) * speed * delta_seconds;
			s_camera.position[0] += forward_x * move;
			s_camera.position[2] += forward_z * move;
		}
		if (abs(cpad.dx) > 15)
		{
			float strafe = ((float)cpad.dx / 156.0f) * speed * delta_seconds;
			s_camera.position[0] += right_x * strafe;
			s_camera.position[2] += right_z * strafe;
		}

		/* Monter / descendre : X, ZR / Y, ZL (L et R servent a la distance) */
		if ((keys & KEY_X) || (keys & KEY_ZR))
			s_camera.position[1] += speed * delta_seconds;
		if ((keys & KEY_Y) || (keys & KEY_ZL))
			s_camera.position[1] -= speed * delta_seconds;
	}
}

static void print_menu(int selected, const char *message)
{
	int first, last, row;

	printf("\x1b[1;1H");
	printf("=== Hitman: Codename 47 ===\x1b[K\n");
	printf("Choisis une mission (%d) :\x1b[K\n\n", s_mission_count);

	first = selected - 7;
	if (first > s_mission_count - 15)
		first = s_mission_count - 15;
	if (first < 0)
		first = 0;
	last = first + 15;
	if (last > s_mission_count)
		last = s_mission_count;

	for (row = first; row < first + 15; ++row)
	{
		if (row < last)
			printf("%c %-34s\x1b[K\n", row == selected ? '>' : ' ', s_mission_names[row]);
		else
			printf("\x1b[K\n");
	}

	printf("\x1b[K\n");
	printf("Haut/Bas : choisir  Gauche/Droite : +-10\x1b[K\n");
	printf("A : jouer          START : quitter\x1b[K\n");
	printf("\x1b[K\n");
	printf("%.39s\x1b[K\n", message);
	printf("\x1b[K\n");
}

static void print_hud(unsigned int fps)
{
	printf("\x1b[1;1H");
	printf("=== Hitman 47 (New 3DS) ===\x1b[K\n");
	printf("Mode  : %s\x1b[K\n",
		s_camera_mode == CAMERA_MODE_THIRD_PERSON ? "[47 - 3eme pers.]" : "[Camera Libre]");
	printf("%s FPS:%u Brouillard:%s\x1b[K\n", s_mission_name, fps, s_fog_enabled ? "ON" : "OFF");
	printf("Distance : %.0f m  (L- / R+)\x1b[K\n", s_view_dist / 100.0f);
	printf("47 : %.0f %.0f %.0f\x1b[K\n",
		s_player.position[0], s_player.position[1], s_player.position[2]);
	printf("Objets: %lu/%lu  Tri:%lu  DC:%lu\x1b[K\n",
		(unsigned long)s_drawn_objects, (unsigned long)s_scene_object_count,
		(unsigned long)s_drawn_triangles, (unsigned long)s_draw_calls);
	printf("Modeles: %lu charges, %lu en echec\x1b[K\n",
		(unsigned long)s_models_loaded, (unsigned long)s_models_failed);
	printf("Textures: %lu ok, %lu manquantes\x1b[K\n",
		(unsigned long)s_tex_loaded, (unsigned long)s_tex_failed);
	printf("Mem GPU libre: %lu Ko\x1b[K\n\x1b[K\n",
		(unsigned long)(linearSpaceFree() / 1024));

	printf("SELECT: mode 47 / cam libre\x1b[K\n");
	printf("A     : brouillard on/off\x1b[K\n");
	printf("L / R : distance - / +\x1b[K\n");
	if (s_camera_mode == CAMERA_MODE_THIRD_PERSON)
	{
		printf("Pad   : deplacer 47\x1b[K\n");
		printf("B     : courir\x1b[K\n");
		printf("C/Croix: tourner la camera\x1b[K\n");
	}
	else
	{
		printf("Pad   : se deplacer\x1b[K\n");
		printf("C     : regarder\x1b[K\n");
		printf("X/ZR  : monter   Y/ZL : descendre\x1b[K\n");
		printf("B     : turbo\x1b[K\n");
	}
	printf("START : retour au menu\x1b[K\n");
}

int main(void)
{
	bool new_3ds = false;
	u64 fps_start;
	u64 previous_frame_time;
	unsigned int frames = 0;
	unsigned int fps = 0;
	int engine_ok = 0;
	int in_menu = 1;
	int selected = 0;
	char menu_msg[256] = "";
	int i;

	gfxInitDefault();
	consoleInit(GFX_BOTTOM, NULL);
	gfxSet3D(false);
	log_init();

	if (R_SUCCEEDED(APT_CheckNew3DS(&new_3ds)) && new_3ds)
		osSetSpeedupEnable(true);

	irrstInit();

	if (!C3D_Init(GPU_CMDBUF_SIZE))
	{
		snprintf(s_error, sizeof(s_error), "Impossible d'initialiser Citro3D.");
		log_message(s_error);
	}
	else
	{
		s_citro3d_initialized = 1;
		s_top_target = C3D_RenderTargetCreate(
			240, 400, GPU_RB_RGBA8, GPU_RB_DEPTH24_STENCIL8);
		if (s_top_target)
			C3D_RenderTargetSetOutput(s_top_target, GFX_TOP,
				GFX_LEFT, DISPLAY_TRANSFER_FLAGS);

		if (!s_top_target)
		{
			snprintf(s_error, sizeof(s_error), "Impossible de creer l'ecran de rendu.");
			log_message(s_error);
		}
		else if (!scene_init())
		{
			snprintf(s_error, sizeof(s_error),
				"Impossible de charger le shader de la scene.");
			log_message(s_error);
		}
		else if (load_mission_list() == 0)
		{
			if (!s_error[0])
			{
				snprintf(s_error, sizeof(s_error),
					"Donnees introuvables : copie le paquet sur la SD.");
				log_message(s_error);
			}
		}
		else
		{
			engine_ok = 1;
			for (i = 0; i < s_mission_count; ++i)
			{
				if (strcmp(s_mission_names[i], DEFAULT_MISSION) == 0)
					selected = i;
			}
		}
	}

	if (!engine_ok)
		snprintf(menu_msg, sizeof(menu_msg), "%s", s_error);

	fps_start = osGetTime();
	previous_frame_time = fps_start;

	while (aptMainLoop())
	{
		u32 keys;
		u32 keys_down;
		circlePosition cpad;
		circlePosition cstick;
		u64 now = osGetTime();
		float delta_seconds = (float)(now - previous_frame_time) / 1000.0f;

		if (delta_seconds > 0.1f)
			delta_seconds = 0.1f;
		previous_frame_time = now;
		++s_frame;

		hidScanInput();
		irrstScanInput();
		keys = hidKeysHeld();
		keys_down = hidKeysDown();
		hidCircleRead(&cpad);
		irrstCstickRead(&cstick);

		if (in_menu)
		{
			if (keys_down & KEY_START)
				break;

			if (engine_ok)
			{
				if (keys_down & KEY_DDOWN)
					selected = (selected + 1) % s_mission_count;
				if (keys_down & KEY_DUP)
					selected = (selected + s_mission_count - 1) % s_mission_count;
				if (keys_down & KEY_DRIGHT)
				{
					selected += 10;
					if (selected >= s_mission_count)
						selected = s_mission_count - 1;
				}
				if (keys_down & KEY_DLEFT)
				{
					selected -= 10;
					if (selected < 0)
						selected = 0;
				}

				if (keys_down & KEY_A)
				{
					if (start_mission(selected))
					{
						in_menu = 0;
						menu_msg[0] = '\0';
					}
					else
					{
						snprintf(menu_msg, sizeof(menu_msg), "%s", s_error);
					}
					printf("\x1b[2J");
					previous_frame_time = osGetTime();
					frames = 0;
					fps_start = previous_frame_time;
				}
			}
		}
		else
		{
			if (keys_down & KEY_START)
			{
				/*
				 * Retour au menu : on attend la fin de la derniere frame GPU
				 * (frame vide) AVANT de liberer la memoire qu'elle utilisait.
				 */
				render_blank_frame();
				scene_unload();
				in_menu = 1;
				menu_msg[0] = '\0';
				printf("\x1b[2J");
			}
			else
			{
				update_play(keys, keys_down, cpad, cstick, delta_seconds);
			}
		}

		++frames;
		if (now - fps_start >= 1000)
		{
			fps = (unsigned int)((u64)frames * 1000 / (now - fps_start));
			frames = 0;
			fps_start = now;
		}

		if (s_citro3d_initialized && C3D_FrameBegin(C3D_FRAME_SYNCDRAW))
		{
			if (s_top_target)
			{
				/*
				 * Chargement / liberation des modeles ICI : FrameBegin a attendu la
				 * fin de la frame precedente, donc le GPU ne lit plus les buffers
				 * qu'on libere. Avant, on le faisait pendant que le GPU dessinait
				 * encore -> textures blanches / geometrie corrompue qui clignote.
				 */
				if (!in_menu)
				{
					u32 free_mem = linearSpaceFree();
					if (free_mem < LOW_LINEAR_EVICT)
					{
						evict_far_models(4);
						evict_unreferenced_textures(4);
					}
					else if (free_mem >= LOW_LINEAR_NOSTREAM)
					{
						stream_models(1, s_view_dist);
					}
				}
				C3D_RenderTargetClear(s_top_target, C3D_CLEAR_ALL,
					CLEAR_COLOR, 0);
				C3D_FrameDrawOn(s_top_target);
				if (!in_menu)
					scene_draw();
			}
			C3D_FrameEnd(0);
		}

		if (in_menu)
			print_menu(selected, menu_msg);
		else
			print_hud(fps);
	}

	if (!in_menu)
		render_blank_frame();
	scene_exit();
	irrstExit();

	if (s_package.file)
		fclose(s_package.file);
	free(s_package.records);
	free(s_package.strings);
	if (s_log)
		fclose(s_log);
	if (s_citro3d_initialized)
		C3D_Fini();
	gfxExit();
	return 0;
}
