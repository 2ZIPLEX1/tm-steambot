using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace SteamWalletChecker;

/// <summary>
/// Конфигурация для хранения учетных данных Steam
/// </summary>
public class SteamConfig
{
    [JsonPropertyName("username")]
    public string Username { get; set; } = string.Empty;

    [JsonPropertyName("password")]
    public string Password { get; set; } = string.Empty;

    [JsonPropertyName("shared_secret")]
    public string? SharedSecret { get; set; }

    [JsonPropertyName("identity_secret")]
    public string? IdentitySecret { get; set; }

    [JsonPropertyName("steamid")]
    public string? SteamId { get; set; }

    [JsonPropertyName("auto_login")]
    public bool AutoLogin { get; set; } = true;

    /// <summary>
    /// Загрузка конфигурации из файла
    /// </summary>
    public static SteamConfig LoadFromFile(string filePath)
    {
        if (!File.Exists(filePath))
        {
            throw new FileNotFoundException($"Config file not found: {filePath}");
        }

        try
        {
            string json = File.ReadAllText(filePath);
            var config = JsonSerializer.Deserialize<SteamConfig>(json);

            if (config == null)
            {
                throw new InvalidOperationException("Failed to deserialize config");
            }

            return config;
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException($"Failed to load config: {ex.Message}", ex);
        }
    }

    /// <summary>
    /// Сохранение конфигурации в файл
    /// </summary>
    public void SaveToFile(string filePath)
    {
        try
        {
            var options = new JsonSerializerOptions
            {
                WriteIndented = true
            };

            string json = JsonSerializer.Serialize(this, options);
            File.WriteAllText(filePath, json);
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException($"Failed to save config: {ex.Message}", ex);
        }
    }

    /// <summary>
    /// Проверка наличия всех необходимых данных
    /// </summary>
    public bool IsValid()
    {
        return !string.IsNullOrEmpty(Username) && !string.IsNullOrEmpty(Password);
    }

    /// <summary>
    /// Проверка наличия данных для автоматической 2FA
    /// </summary>
    public bool HasAutoAuth()
    {
        return !string.IsNullOrEmpty(SharedSecret);
    }

    /// <summary>
    /// Создание примера конфигурации
    /// </summary>
    public static void CreateExampleConfig(string filePath)
    {
        var exampleConfig = new SteamConfig
        {
            Username = "your_steam_username",
            Password = "your_steam_password",
            SharedSecret = "your_shared_secret_base64",
            IdentitySecret = "your_identity_secret_base64",
            SteamId = "your_steam_id_64",
            AutoLogin = true
        };

        exampleConfig.SaveToFile(filePath);
        Console.WriteLine($"✓ Example config created: {filePath}");
        Console.WriteLine("⚠️ Please edit this file with your actual credentials");
    }
}

/// <summary>
/// Менеджер для работы с несколькими конфигурациями
/// </summary>
public class SteamConfigManager
{
    private readonly string configDirectory;

    public SteamConfigManager(string? configDirectory = null)
    {
        this.configDirectory = configDirectory ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "SteamWalletChecker"
        );

        if (!Directory.Exists(this.configDirectory))
        {
            Directory.CreateDirectory(this.configDirectory);
        }
    }

    /// <summary>
    /// Получение пути к конфигу по имени
    /// </summary>
    public string GetConfigPath(string name = "default")
    {
        return Path.Combine(configDirectory, $"{name}.json");
    }

    /// <summary>
    /// Загрузка конфига по имени
    /// </summary>
    public SteamConfig LoadConfig(string name = "default")
    {
        string path = GetConfigPath(name);
        return SteamConfig.LoadFromFile(path);
    }

    /// <summary>
    /// Сохранение конфига
    /// </summary>
    public void SaveConfig(SteamConfig config, string name = "default")
    {
        string path = GetConfigPath(name);
        config.SaveToFile(path);
    }

    /// <summary>
    /// Получение списка всех конфигов
    /// </summary>
    public string[] GetAllConfigs()
    {
        var files = Directory.GetFiles(configDirectory, "*.json");
        return Array.ConvertAll(files, f => Path.GetFileNameWithoutExtension(f));
    }

    /// <summary>
    /// Проверка существования конфига
    /// </summary>
    public bool ConfigExists(string name = "default")
    {
        return File.Exists(GetConfigPath(name));
    }
}
