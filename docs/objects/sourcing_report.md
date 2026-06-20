# Skopje Landmark Object Sourcing Report – Condor 2 Landscape

**Project:** `D:/Repos/condor-landscape`  
**Report date:** 2026-06-13  
**Scope:** Find freely usable 3D models for five Skopje-area features and document the Condor 2 custom-object pipeline.

---

## 1. Executive summary

| Landmark | Free model found? | Best candidate | License / blocker |
|---|---|---|---|
| **Millennium Cross** | No | Must be custom-modelled or use a generic cross | No landmark-specific free asset |
| **Stone Bridge** | Yes (3D Warehouse) | ViktorMKD "Stone Bridge-Skopje" (SKP) | Trimble General Model License – OK in a combined work, **not** as a standalone redistribution |
| **Skopje City Tower / Limak Towers** | Generic only | 3D Warehouse "Tower in Skopje 401" (SKP) | Trimble General Model License; ambiguous which real building is meant |
| **Vodno telecom tower** | Generic only | Sketchfab "Telecommunication Tower Low-Poly Free" | CC BY 4.0 – attribution required |
| **MSFS 2020 Stenkovec objects** | No | SoFly payware scenery | Extraction/redistribution prohibited by EULA |

**Key take-away:** only the Stone Bridge has a recognizable free model. The other landmarks either need custom modelling, a generic placeholder, or (for Stenkovec) completely new assets built from imagery. Any 3D Warehouse model used must remain embedded in the final landscape package and not be exposed as a stand-alone download.

---

## 2. Condor 2 custom-object pipeline

Based on `docs/condor_file_formats.md`, the installed `Slovenia2` sample, and the *Condor Landscape Guide ENGLISH rev2*.

### 2.1 Files involved

```text
<LandscapeName>/
├── <LandscapeName>.obj       # binary placement file (152-byte records)
└── World/
    ├── Objects/*.c3d         # shared 3D models
    └── Textures/*.dds        # object textures
```

### 2.2 High-level workflow

1. **Model** in Blender, Wings3D, or 3ds Max.
2. **Export** a triangulated Wavefront `.obj` + `.mtl`.
   - One material per mesh part.
   - Include normals and UVs.
   - Use relative texture paths.
3. **Convert textures** to `.dds` (DXT1/DXT3, power-of-two, e.g. 1024×1024 or 2048×2048).
4. **Open the OBJ in `ObjectEditor.exe`** (`tools/CLT2.7/`).
   - Set material RGB to `1.0 / 1.0 / 1.0` for textured parts.
   - Save as `.c3d`.
5. **Place the object** with `LandscapeEditor.exe` → `View/Modify -> Objects`, or edit the `.obj` placement file with `condor_obj_file_tool.py`.

### 2.3 Critical gotchas

- **Quads/ngons crash ObjectEditor.** Triangulate before export.
- Modern Blender `.obj` exporters sometimes produce `.mtl` syntax that ObjectEditor rejects; the older Blender exporter or Wings3D is known to work.
- Object textures go in `World/Textures/` (not in the same folder as the `.c3d`).

---

## 3. Candidate sources per landmark

### 3.1 Millennium Cross (Vodno)

| Attribute | Value |
|---|---|
| Location | Krstovar peak, Mount Vodno, ~41.9650°N 21.3942°E |
| Height | 66 m (structure); peak elevation ~1,066 m ASL |
| Free model | **None found.** |
| Alternatives | Generic cross models on Thingiverse/Cults/Sketchfab (often CC BY); create a simple steel truss cross in Blender. The SoFly MSFS Stenkovec scenery includes a Millennium Cross POI, but it is payware and cannot be extracted/redistributed. |
| Recommended path | Custom model in Blender → triangulated OBJ → ObjectEditor → `.c3d`. Use a 512×512 or 1024×1024 steel/white DDS texture. |

### 3.2 Stone Bridge (Kamen Most)

| Attribute | Value |
|---|---|
| Location | Over the Vardar, ~41.9965°N 21.4335°E |
| Dimensions | ~214 m long, ~6.3 m wide, 13–14 arches |
| Candidate A (free) | **3D Warehouse – "Stone Bridge-Skopje/Kamen most-Skopje" by ViktorMKD** |
| URL | <https://3dwarehouse.sketchup.com/model/u58a00c56-b0f8-43fa-bbec-bcb097d52e98/Stone-Bridge-SkopjeKamen-most-Skopje> |
| Format | `.skp` (SketchUp) |
| License | Trimble 3D Warehouse Terms of Use / General Model License (https://3dwarehouse.sketchup.com/tos). Models may be used in a “Combined Work”; they may **not** be transferred or sold as stand-alone items. |
| Conversion | Download `.skp` → open in SketchUp Free/Pro or Blender with a SketchUp importer → export `.dae`/`.obj` → triangulate → export clean OBJ/MTL → ObjectEditor. |
| Candidate B (paid) | **Sketchfab/Fab – "Skopje Stone Bridge minimal" by Jules Camille Huvig** |
| URL | <https://sketchfab.com/3d-models/skopje-stone-bridge-minimal-e3338583a3f34e7d8ab96a681a515020> |
| Format | OBJ/FBX/GLB after purchase |
| License | Sketchfab Standard/Fab store license (paid, royalty-free after purchase) |
| Notes | ~6.4k triangles, minimalist but recognizable. Ready to convert if budget allows. |

### 3.3 Skopje City Tower / Limak Towers

| Attribute | Value |
|---|---|
| Note | The term “City Tower” is ambiguous. The Emporis/Wikipedia-derived lists show a 20-floor “City Tower” (~2014) and the separate 142 m Cevahir Towers. “Limak” in Skopje refers mainly to the Limak Skopje Luxury Hotel, not a skyscraper. |
| Free model | **No specific model found.** |
| Generic candidate | **3D Warehouse – "Tower in Skopje 401"** (and the similar #400) |
| URL | <https://3dwarehouse.sketchup.com/model/a23ed3eb9a4d336ba3134befbab2c220/Tower-in-Skopje-401> |
| Format | `.skp` |
| License | Trimble General Model License |
| Dimensions | ~159 × 185 × 58 m (bounds from 3D Warehouse) |
| Alternative | Use **OSM2World** to generate massing models from OpenStreetMap building footprints/heights for central Skopje (ODbL license, attribution + share-alike for the database). |
| Recommended path | Decide which real-world building is intended, then either model it from photos or use OSM2World massing as a placeholder. |


### 3.4 Vodno telecom tower

| Attribute | Value |
|---|---|
| Location | Near Millennium Cross, ~41.9635°N 21.3925°E |
| Height | 155 m (concrete column + lattice/antenna), base elevation ~1,066 m ASL |
| Free model | **None found for the actual Vodno tower.** |
| Generic candidate | **Sketchfab – "Telecommunication Tower Low-Poly Free" by Nicholas-3D** |
| URL | <https://sketchfab.com/3d-models/telecommunication-tower-low-poly-free-39bee442b9aa4c3d8dc7674453cd78ad> |
| Format | OBJ/GLB/FBX |
| License | **CC BY 4.0** – attribution required |
| Notes | Six variants, low-poly, game-ready. Download, scale to 155 m, recolour/retexture to match the concrete Vodno tower, and credit the author. |

### 3.5 MSFS 2020 Stenkovec (LW75) airport objects

| Attribute | Value |
|---|---|
| Product | SoFly – **Skopje Airfield Collection / Stenkovec Brazda Airport LW75** |
| URLs | <https://sofly.io/products/stenkovec-brazda-airport-lw75> and <https://sofly.io/products/skopje-airfield-collection-for-msfs-trio-pack> |
| Price | ~£5.99–£7.99 (payware) |
| Extraction feasibility | Technically possible with tools such as ModelConverterX or package unpackers for personal/local use. |
| Legal blocker | SoFly and Microsoft Flight Simulator EULAs prohibit reverse engineering, extraction, and redistribution of assets. The scenery also includes a custom Millennium Cross POI. |
| Recommendation | **Do not extract.** Build generic hangar, windsock, and tower objects from photos/satellite imagery, or use free CC0/CC BY airport object libraries. |

---

## 4. License & redistribution matrix

| Source | Typical license | Usable in Condor landscape? | Redistributable? |
|---|---|---|---|
| 3D Warehouse (Stone Bridge, generic towers) | Trimble General Model License | Yes, as part of a Combined Work | Only as part of the finished landscape; **not** as a stand-alone model download |
| Sketchfab free CC BY assets (generic telecom tower) | CC BY 4.0 | Yes, with attribution | Yes, with attribution |
| Sketchfab/Fab paid (Stone Bridge minimal) | Sketchfab Standard/Fab | Yes, after purchase | Check specific license; generally yes for derivative game content |
| MSFS / SoFly scenery | Commercial EULA | No for our use case | No |
| Google 3D Tiles / Google Earth | Google ToU | No | No |
| OSM2World generated massing | ODbL | Yes | Yes, with OSM attribution and share-alike for the database |

---

## 5. Recommended next steps

1. **Stone Bridge** – download the free 3D Warehouse SKP, check polygon count, scale it to ~214 m length, convert to triangulated OBJ, and test in `ObjectEditor.exe`. Keep the source `.skp` private; only ship the final `.c3d` inside the landscape package.
2. **Millennium Cross** – model a simple 66 m steel truss cross in Blender (low-poly, LOD-friendly) and convert to `.c3d`.
3. **Vodno telecom tower** – download the Sketchfab generic telecom tower, scale to 155 m, retexture, credit Nicholas-3D in `ATTRIBUTION.md`.
4. **City Tower** – clarify which real-world building is required, then either model from reference photos or use OSM2World massing.
5. **Stenkovec** – create generic airport objects locally; do **not** attempt to extract from MSFS.
6. Create `sources/objects/ATTRIBUTION.md` listing every author, source URL, and license, to satisfy the attribution requirements of CC BY and ODbL assets.

---

## 6. Tools & references

- Condor file format reference: `D:/Repos/condor-landscape/docs/condor_file_formats.md`
- Condor Landscape Guide ENGLISH rev2: <https://www.condorsoaring.com/wp-content/downloads/clt/Condor%20Landscape%20Guide%20ENGLISH%20rev2.pdf>
- Condor ObjectEditor `.obj` triangulation notes: <https://www.condorsoaring.com/forums/viewtopic.php?t=17727&start=15>
- 3D Warehouse Terms of Use / General Model License: <https://3dwarehouse.sketchup.com/tos>
- 3D Warehouse license FAQ: <https://help.sketchup.com/en/3d-warehouse/3d-warehouse-terms-use-faq>
- Sketchfab licenses: <https://sketchfab.com/licenses>
- OSM2World: <https://osm2world.org/>
- OpenStreetMap license (ODbL): <https://opendatacommons.org/licenses/odbl/>
- SoFly Stenkovec product page: <https://sofly.io/products/stenkovec-brazda-airport-lw75>
