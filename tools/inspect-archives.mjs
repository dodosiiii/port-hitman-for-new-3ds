import fs from "node:fs";
import path from "node:path";

const inputPath = path.resolve(process.argv[2] ?? "Hitman Codename 47");
const outputPath = process.argv[3] ? path.resolve(process.argv[3]) : null;

if (!fs.existsSync(inputPath)) {
  throw new Error(`Input path does not exist: ${inputPath}`);
}
const inputStat = fs.statSync(inputPath);
const singleArchive = inputStat.isFile();
const root = singleArchive ? path.dirname(inputPath) : inputPath;

function findArchives(directory) {
  const archives = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      archives.push(...findArchives(entryPath));
    } else if (entry.isFile() && path.extname(entry.name).toLowerCase() === ".zip") {
      archives.push(entryPath);
    }
  }
  return archives.sort((a, b) => a.localeCompare(b));
}

function parseZip(filePath) {
  const buffer = fs.readFileSync(filePath);
  const searchStart = Math.max(0, buffer.length - 22 - 0xffff);
  let eocd = -1;

  for (let offset = buffer.length - 22; offset >= searchStart; offset -= 1) {
    if (buffer.readUInt32LE(offset) === 0x06054b50) {
      const commentLength = buffer.readUInt16LE(offset + 20);
      if (offset + 22 + commentLength <= buffer.length) {
        eocd = offset;
        break;
      }
    }
  }

  if (eocd < 0) {
    throw new Error("ZIP end-of-central-directory record not found");
  }

  const diskNumber = buffer.readUInt16LE(eocd + 4);
  const centralDirectoryDisk = buffer.readUInt16LE(eocd + 6);
  const entriesOnDisk = buffer.readUInt16LE(eocd + 8);
  const entryCount = buffer.readUInt16LE(eocd + 10);
  const centralDirectorySize = buffer.readUInt32LE(eocd + 12);
  const centralDirectoryOffset = buffer.readUInt32LE(eocd + 16);

  if (diskNumber !== 0 || centralDirectoryDisk !== 0 || entriesOnDisk !== entryCount) {
    throw new Error("Multi-disk ZIP archives are not supported");
  }
  if (
    entryCount === 0xffff ||
    centralDirectorySize === 0xffffffff ||
    centralDirectoryOffset === 0xffffffff
  ) {
    throw new Error("ZIP64 archives are not supported by this inventory tool");
  }
  if (centralDirectoryOffset + centralDirectorySize > buffer.length) {
    throw new Error("Central directory extends beyond the end of the archive");
  }

  const entries = [];
  let offset = centralDirectoryOffset;

  for (let index = 0; index < entryCount; index += 1) {
    if (offset + 46 > buffer.length || buffer.readUInt32LE(offset) !== 0x02014b50) {
      throw new Error(`Invalid central directory entry ${index}`);
    }

    const flags = buffer.readUInt16LE(offset + 8);
    const compressedSize = buffer.readUInt32LE(offset + 20);
    const uncompressedSize = buffer.readUInt32LE(offset + 24);
    const nameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const end = offset + 46 + nameLength + extraLength + commentLength;

    if (end > buffer.length) {
      throw new Error(`Truncated central directory entry ${index}`);
    }
    if (compressedSize === 0xffffffff || uncompressedSize === 0xffffffff) {
      throw new Error("ZIP64 entries are not supported by this inventory tool");
    }

    const nameBytes = buffer.subarray(offset + 46, offset + 46 + nameLength);
    const encoding = flags & 0x0800 ? "utf-8" : "windows-1252";
    const name = new TextDecoder(encoding).decode(nameBytes);

    if (!name.endsWith("/")) {
      entries.push({ name, compressedSize, uncompressedSize });
    }
    offset = end;
  }

  if (offset !== centralDirectoryOffset + centralDirectorySize) {
    throw new Error("Central directory size does not match its entries");
  }
  return entries;
}

if (!singleArchive && !inputStat.isDirectory()) {
  throw new Error(`Input path is neither a directory nor a ZIP archive: ${inputPath}`);
}
if (singleArchive && path.extname(inputPath).toLowerCase() !== ".zip") {
  throw new Error(`Input file is not a ZIP archive: ${inputPath}`);
}

const archives = singleArchive ? [inputPath] : findArchives(root);
const lines = [`Archives ZIP: ${archives.length}`, `Dossier du jeu: ${root}`, ""];

for (const archive of archives) {
  const entries = parseZip(archive);
  const extensionCounts = new Map();
  let uncompressedTotal = 0;

  for (const entry of entries) {
    const extension = path.posix.extname(entry.name).toLowerCase() || "[sans extension]";
    extensionCounts.set(extension, (extensionCounts.get(extension) ?? 0) + 1);
    uncompressedTotal += entry.uncompressedSize;
  }

  const summary = [...extensionCounts]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([extension, count]) => `${extension}:${count}`)
    .join(", ");
  lines.push(
    `${path.relative(root, archive)} | ${entries.length} fichiers | ${uncompressedTotal} octets decompresses | ${summary}`,
  );

  for (const entry of entries) {
    lines.push(
      `  ${entry.name} | ${entry.uncompressedSize} octets | ${entry.compressedSize} octets compresses`,
    );
  }
}

const report = `${lines.join("\n")}\n`;
if (outputPath) {
  fs.writeFileSync(outputPath, report, "utf8");
  console.log(`Inventaire ecrit : ${outputPath}`);
  console.log(`Archives : ${archives.length}`);
} else {
  process.stdout.write(report);
}
