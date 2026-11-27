using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using BepInEx;
using BepInEx.Logging;
using HarmonyLib;
using TMPro;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.ResourceManagement.AsyncOperations;
using UnityEngine.TextCore.LowLevel;

[BepInProcess("AThousandCloys.exe")]
[BepInPlugin(PluginId, "A Thousand Cloys Russian Translation", "3.0.0")]
public sealed class RussianTranslation : BaseUnityPlugin
{
    private const string PluginId = "com.acrom7.atc.russian";
    private const string PixelFontPath = "Assets/Fonts/nihonium113.regular.ttf";
    private const string Cyrillic = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя";
    private static readonly Dictionary<string, char> physicalDefaults = new Dictionary<string, char>
    {
        { "BuildCloy", 'C' }, { "SelectPortal", 'P' }, { "CycleBeacons", 'B' },
        { "OpenMails", 'M' }, { "CycleRemoteBase", 'R' },
    };
    private static Dictionary<string, Translation> translations;
    private static ManualLogSource log;
    private static MethodInfo getManager;
    private static FieldInfo languages;
    private static MethodInfo getInputAsset;
    private static TMP_FontAsset fallback;

    private void Awake()
    {
        if (!Config.Bind("General", "Enabled", true, "Use Russian when the game is set to English. Restart after changing.").Value)
            return;

        log = Logger;
        try
        {
            var path = Path.Combine(Path.GetDirectoryName(Info.Location), "strings.json");
            using (var file = File.OpenRead(path))
            {
                var reader = new DataContractJsonSerializer(typeof(Translation[]));
                translations = ((Translation[])reader.ReadObject(file)).ToDictionary(row => row.Key, StringComparer.Ordinal);
            }
            if (translations.Count == 0 || translations.Values.Any(row =>
                string.IsNullOrWhiteSpace(row.Key) || string.IsNullOrWhiteSpace(row.Text) ||
                row.Source == null || row.Source.Length != 64 || row.Source.Any(c => !Uri.IsHexDigit(c))))
                throw new InvalidDataException("Invalid translation table");

            var manager = AccessTools.TypeByName("LocalizationManager");
            if (manager == null)
                throw new MissingMemberException("LocalizationManager");

            var load = AccessTools.DeclaredMethod(manager, "OnAsyncLoadDone", new[] { typeof(AsyncOperationHandle<TextAsset>) });
            getManager = AccessTools.PropertyGetter(manager.BaseType, "Instance");
            languages = AccessTools.DeclaredField(manager, "languages");
            if (load == null || !load.IsStatic || load.ReturnType != typeof(void) ||
                getManager == null || !getManager.IsStatic || getManager.ReturnType != manager ||
                languages == null || languages.IsStatic || languages.IsInitOnly ||
                languages.FieldType != typeof(Dictionary<string, Dictionary<int, string>>))
                throw new MissingMemberException("Unsupported localization interface");

            var harmony = new Harmony(PluginId);
            harmony.Patch(load, postfix: new HarmonyMethod(typeof(RussianTranslation), "AfterLoad"));
            PatchKeyboardDefaults(harmony);
        }
        catch (Exception error)
        {
            Logger.LogWarning("Russian translation disabled: " + error.Message);
        }
    }

    private static void PatchKeyboardDefaults(Harmony harmony)
    {
        try
        {
            var input = AccessTools.TypeByName("InputActions");
            if (input == null)
                return;

            var constructor = AccessTools.DeclaredConstructor(input, Type.EmptyTypes);
            getInputAsset = AccessTools.PropertyGetter(input, "asset");
            if (constructor == null || getInputAsset == null || getInputAsset.IsStatic ||
                getInputAsset.ReturnType != typeof(InputActionAsset))
                return;

            harmony.Patch(constructor, postfix: new HarmonyMethod(typeof(RussianTranslation), "AfterInputCreated"));
        }
        catch (Exception error)
        {
            log.LogWarning("Keyboard defaults left unchanged: " + error.Message);
        }
    }

    private static void AfterInputCreated(object __instance)
    {
        try
        {
            var asset = (InputActionAsset)getInputAsset.Invoke(__instance, null);
            int repaired = 0;
            foreach (var action in asset)
            {
                char letter;
                if (!physicalDefaults.TryGetValue(action.name, out letter))
                    continue;

                // Display-name bindings stop resolving when the user switches to Cyrillic.
                var displayPath = "<Keyboard>/#(" + letter + ")";
                for (int i = 0; i < action.bindings.Count; i++)
                {
                    if (action.bindings[i].path != displayPath)
                        continue;

                    action.ChangeBinding(i).WithPath("<Keyboard>/" + char.ToLowerInvariant(letter));
                    repaired++;
                }
            }
            if (repaired > 0)
                log.LogInfo("Repaired default keyboard bindings: " + repaired);
        }
        catch (Exception error)
        {
            log.LogWarning("Could not apply keyboard defaults: " + error.Message);
        }
    }

    private static void AfterLoad(AsyncOperationHandle<TextAsset> __0)
    {
        try
        {
            if (!__0.IsValid() || __0.Status != AsyncOperationStatus.Succeeded)
                return;

            var manager = getManager.Invoke(null, null);
            var original = (Dictionary<string, Dictionary<int, string>>)languages.GetValue(manager);
            var updated = new Dictionary<string, Dictionary<int, string>>(original, original.Comparer);
            int applied = 0;
            using (var sha = SHA256.Create())
            {
                foreach (var entry in original)
                {
                    string english;
                    Translation translation;
                    if (!entry.Value.TryGetValue(0, out english) || string.IsNullOrEmpty(english) ||
                        !translations.TryGetValue(entry.Key, out translation))
                        continue;

                    var source = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(english))).Replace("-", "");
                    if (!string.Equals(source, translation.Source, StringComparison.OrdinalIgnoreCase))
                        continue;

                    var row = new Dictionary<int, string>(entry.Value);
                    row[0] = translation.Text;
                    updated[entry.Key] = row;
                    applied++;
                }
            }

            if (applied == 0)
            {
                log.LogWarning("Russian translation disabled: no source strings matched");
                return;
            }

            EnsureFont();
            languages.SetValue(manager, updated);
            log.LogInfo(string.Format("Russian translation: {0} strings applied; {1} original entries retained", applied, original.Count - applied));
        }
        catch (Exception error)
        {
            log.LogWarning("Russian translation skipped: " + error.Message);
        }
    }

    private static void EnsureFont()
    {
        if (!fallback)
        {
            try
            {
                fallback = CreatePixelFont();
            }
            catch (Exception error)
            {
                log.LogWarning("Could not use the game font: " + error.Message);
            }

            if (!fallback)
            {
                fallback = CreateCyrillicFont(TMP_FontAsset.CreateFontAsset("Arial", "Regular", 64), "ATC Russian Arial");
                if (!fallback)
                    throw new InvalidOperationException("Cyrillic fallback font unavailable");
                log.LogWarning("The game font is unavailable, using Arial for Russian text");
            }
        }

        var fonts = TMP_Settings.fallbackFontAssets;
        if (fonts == null)
            TMP_Settings.fallbackFontAssets = new List<TMP_FontAsset> { fallback };
        else if (!fonts.Contains(fallback))
            fonts.Add(fallback);
    }

    private static TMP_FontAsset CreatePixelFont()
    {
        // The game has the font bundle loaded by the time it loads its localization table.
        var bundle = AssetBundle.GetAllLoadedAssetBundles().FirstOrDefault(asset => asset.Contains(PixelFontPath));
        var source = bundle ? bundle.LoadAsset<Font>(PixelFontPath) : null;
        if (!source)
            return null;

        var font = TMP_FontAsset.CreateFontAsset(source, 32, 14, GlyphRenderMode.SDF32, 1024, 1024, AtlasPopulationMode.Dynamic, false);
        if (!font)
            return null;

        var face = font.faceInfo;
        face.scale = 0.85f;
        font.faceInfo = face;
        var cyrillic = CreateCyrillicFont(font, "ATC Russian Nihonium113");
        if (cyrillic)
            log.LogInfo("Using the game's Nihonium113 font for Russian text");
        return cyrillic;
    }

    private static TMP_FontAsset CreateCyrillicFont(TMP_FontAsset font, string name)
    {
        if (!font)
            return null;
        font.name = name;
        if (font.TryAddCharacters(Cyrillic))
            return font;
        Destroy(font);
        return null;
    }

    [DataContract]
    private sealed class Translation
    {
        [DataMember(Name = "key", IsRequired = true)]
        public string Key { get; set; }

        [DataMember(Name = "source", IsRequired = true)]
        public string Source { get; set; }

        [DataMember(Name = "text", IsRequired = true)]
        public string Text { get; set; }
    }
}
