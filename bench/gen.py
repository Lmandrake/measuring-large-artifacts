"""Generate a synthetic def-dump file shaped like the real one.

Single-line JSON: {"defType":X,"defTypeFull":Y,"defs":[...],"count":N}
Records carry the fields dumpdb.build actually reads.
"""
import json, os, random, sys

out = sys.argv[1]
target_mb = float(sys.argv[2]) if len(sys.argv) > 2 else 64
nonascii = "--ascii" not in sys.argv

random.seed(7)
MODS = [("Core", "ludeon.rimworld"), ("Vanilla Expanded", "vanillaexpanded.vee"),
        ("Jawa", "mandrake.jawa"), ("Combat Extended", "ceteam.combatextended")]
TAGS = ["Gun", "GunSingleUse", "SimpleGun", "IndustrialGunAdvanced", "Neolithic",
        "Bladelink", "SpacerGun", "AssaultRifle"]
LABELS = ["revolver", "autopistol", "heavy SMG", "chain shotgun"]
if nonascii:
    LABELS += ["fusil d’assaut", "プラズマガン", "sabre laser"]


def rec(i):
    mod, pkg = MODS[i % len(MODS)]
    return {
        "defName": "Gun_Synth%06d" % i,
        "defType": "ThingDef",
        "defTypeFull": "Verse.ThingDef",
        "label": "%s %d" % (LABELS[i % len(LABELS)], i),
        "description": "A synthetic record padded to a realistic size. " * 6,
        "modName": mod,
        "packageId": pkg,
        "shortHash": random.randint(1, 65535),
        "fields": {
            "weaponTags": random.sample(TAGS, k=random.randint(1, 3)),
            "tradeTags": ["Weapon"],
            "techLevel": "Industrial",
            "statBases": {"Mass": 2.5, "MarketValue": 220.0, "Accuracy": 0.85},
            "comps": [{"compClass": "CompQuality"}, {"compClass": "CompBiocodable"}],
            "verbs": [{"verbClass": "Verb_Shoot", "warmupTime": 0.3,
                       "range": 26.9, "burstShotCount": 1}],
        },
        "is": {"IsWeapon": True, "IsApparel": False, "IsMeleeWeapon": False,
               "IsRangedWeapon": True, "IsMedicine": False},
    }


target = int(target_mb * 1024 * 1024)
with open(out, "w", encoding="utf-8") as fh:
    fh.write('{"defType":"ThingDef","defTypeFull":"Verse.ThingDef","defs":[')
    n = 0
    written = fh.tell() if fh.seekable() else 0
    size = 0
    while size < target:
        chunk = []
        for _ in range(2000):
            chunk.append(json.dumps(rec(n), separators=(",", ":"),
                                    ensure_ascii=False))
            n += 1
        s = ("," if size else "") + ",".join(chunk)
        fh.write(s)
        size += len(s.encode("utf-8"))
    fh.write('],"count":%d}' % n)
print("%s  %d defs  %.1f MB" % (out, n, os.path.getsize(out) / 1048576))
