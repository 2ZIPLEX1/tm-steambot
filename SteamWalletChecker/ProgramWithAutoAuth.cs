using System.Text.Json;
using SteamKit2;
using SteamKit2.Authentication;
using SteamKit2.Internal;

namespace SteamWalletChecker;

/// <summary>
/// Версия программы с автоматической 2FA аутентификацией
/// </summary>
class ProgramWithAutoAuth
{
    static SteamClient? steamClient;
    static CallbackManager? manager;
    static SteamUser? steamUser;
    static SteamUnifiedMessages? steamUnifiedMessages;
    static bool isRunning = true;
    static SteamConfig? config;

    static void Main(string[] args)
    {
        Console.WriteLine("=== Steam Wallet Checker (Auto 2FA) ===");
        Console.WriteLine();

        // Парсинг аргументов командной строки
        string configPath = ParseArguments(args);

        // Загрузка конфигурации
        if (!LoadConfiguration(configPath))
        {
            return;
        }

        // Проверка конфигурации
        if (!config!.IsValid())
        {
            Console.WriteLine("❌ Invalid configuration: username and password are required");
            return;
        }

        // Информация о режиме аутентификации
        if (config.HasAutoAuth() && !string.IsNullOrEmpty(config.SharedSecret))
        {
            Console.WriteLine("✓ Auto 2FA authentication enabled");
            Console.WriteLine($"✓ Using shared_secret: {config.SharedSecret[..Math.Min(10, config.SharedSecret.Length)]}...");
        }
        else
        {
            Console.WriteLine("⚠️ Auto 2FA not configured - will require manual code input");
        }

        Console.WriteLine();

        // Инициализация клиента
        steamClient = new SteamClient();
        manager = new CallbackManager(steamClient);
        steamUser = steamClient.GetHandler<SteamUser>();
        steamUnifiedMessages = steamClient.GetHandler<SteamUnifiedMessages>();

        // Подписка на события
        manager.Subscribe<SteamClient.ConnectedCallback>(OnConnected);
        manager.Subscribe<SteamClient.DisconnectedCallback>(OnDisconnected);
        manager.Subscribe<SteamUser.LoggedOnCallback>(OnLoggedOn);
        manager.Subscribe<SteamUser.LoggedOffCallback>(OnLoggedOff);

        Console.WriteLine($"Connecting to Steam as '{config.Username}'...");
        steamClient.Connect();

        // Основной цикл обработки событий
        while (isRunning)
        {
            manager.RunWaitCallbacks(TimeSpan.FromSeconds(1));
        }

        // Exit immediately without waiting for keypress
        Console.WriteLine();
        Console.WriteLine("Exiting...");
    }

    static string ParseArguments(string[] args)
    {
        // По умолчанию ищем steam_account.json в текущей директории
        string defaultPath = "steam_account.json";

        if (args.Length > 0)
        {
            if (args[0] == "--help" || args[0] == "-h")
            {
                ShowHelp();
                Environment.Exit(0);
            }

            if (args[0] == "--create-example")
            {
                SteamConfig.CreateExampleConfig("steam_account.example.json");
                Environment.Exit(0);
            }

            // Первый аргумент - путь к конфигу
            defaultPath = args[0];
        }

        return defaultPath;
    }

    static void ShowHelp()
    {
        Console.WriteLine(@"
Steam Wallet Checker - Auto 2FA Version

Usage:
  SteamWalletChecker [config_file]
  SteamWalletChecker --help
  SteamWalletChecker --create-example

Arguments:
  config_file          Path to JSON config file (default: steam_account.json)
  --help, -h          Show this help message
  --create-example    Create example config file

Config file format (JSON):
{
  ""username"": ""your_steam_username"",
  ""password"": ""your_steam_password"",
  ""shared_secret"": ""your_shared_secret_base64"",
  ""identity_secret"": ""your_identity_secret_base64"",
  ""steamid"": ""your_steam_id_64"",
  ""auto_login"": true
}

How to get shared_secret and identity_secret:
1. Use SDA (Steam Desktop Authenticator): https://github.com/Jessecar96/SteamDesktopAuthenticator
2. Export your account data
3. Copy shared_secret and identity_secret from the maFile

Examples:
  SteamWalletChecker
  SteamWalletChecker my_account.json
  SteamWalletChecker accounts/main_account.json
");
    }

    static bool LoadConfiguration(string configPath)
    {
        try
        {
            if (!File.Exists(configPath))
            {
                Console.WriteLine($"❌ Config file not found: {configPath}");
                Console.WriteLine();
                Console.WriteLine("Create config file with:");
                Console.WriteLine("  SteamWalletChecker --create-example");
                Console.WriteLine();
                Console.WriteLine("Or create manually:");
                SteamConfig.CreateExampleConfig("steam_account.example.json");
                return false;
            }

            config = SteamConfig.LoadFromFile(configPath);
            Console.WriteLine($"✓ Loaded config from: {configPath}");
            return true;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"❌ Failed to load config: {ex.Message}");
            return false;
        }
    }

    static async void OnConnected(SteamClient.ConnectedCallback callback)
    {
        Console.WriteLine($"Connected to Steam! Logging in as '{config!.Username}'...");

        try
        {
            // Создание аутентификатора
            var authenticator = new SteamGuardAuthenticator(
                config.SharedSecret,
                config.IdentitySecret
            );

            // Валидация shared_secret (если есть)
            if (config.HasAutoAuth() && !authenticator.ValidateSharedSecret())
            {
                Console.WriteLine("⚠️ Warning: shared_secret might be invalid");
            }

            // Начало аутентификации
            var authSession = await steamClient!.Authentication.BeginAuthSessionViaCredentialsAsync(new AuthSessionDetails
            {
                Username = config.Username,
                Password = config.Password,
                Authenticator = authenticator,
            });

            // Ожидание результата
            var pollResponse = await authSession.PollingWaitForResultAsync();

            Console.WriteLine("✓ Authentication successful!");

            // Вход в систему
            steamUser!.LogOn(new SteamUser.LogOnDetails
            {
                Username = pollResponse.AccountName,
                AccessToken = pollResponse.RefreshToken,
            });
        }
        catch (Exception ex)
        {
            Console.WriteLine($"❌ Authentication error: {ex.Message}");
            Console.WriteLine($"   Stack trace: {ex.StackTrace}");
            isRunning = false;
        }
    }

    static void OnDisconnected(SteamClient.DisconnectedCallback callback)
    {
        Console.WriteLine("Disconnected from Steam");
        isRunning = false;
    }

    static async void OnLoggedOn(SteamUser.LoggedOnCallback callback)
    {
        if (callback.Result != EResult.OK)
        {
            if (callback.Result == EResult.AccountLogonDenied)
            {
                Console.WriteLine("❌ Unable to logon: Account is SteamGuard protected");
                Console.WriteLine("   Make sure your shared_secret is correct");
                isRunning = false;
                return;
            }

            if (callback.Result == EResult.InvalidPassword)
            {
                Console.WriteLine("❌ Unable to logon: Invalid password");
                isRunning = false;
                return;
            }

            if (callback.Result == EResult.RateLimitExceeded)
            {
                Console.WriteLine("❌ Unable to logon: Rate limit exceeded");
                Console.WriteLine("   Please wait a few minutes before trying again");
                isRunning = false;
                return;
            }

            Console.WriteLine($"❌ Unable to logon: {callback.Result} / {callback.ExtendedResult}");
            isRunning = false;
            return;
        }

        Console.WriteLine("✓ Successfully logged on!");
        Console.WriteLine();

        try
        {
            // Получение информации о кошельке
            Console.WriteLine("Fetching wallet information...");

            var userService = steamUnifiedMessages!.CreateService<UserAccount>();

            var wallet = await userService.GetClientWalletDetails(new CUserAccount_GetClientWalletDetails_Request
            {
                include_balance_in_usd = true,
                include_formatted_balance = true
            });

            // Output JSON directly to stdout (Python will parse this)
            Console.WriteLine();
            Console.WriteLine("=== WALLET_JSON_START ===");
            Console.WriteLine(JsonSerializer.Serialize(wallet, new JsonSerializerOptions
            {
                WriteIndented = true
            }));
            Console.WriteLine("=== WALLET_JSON_END ===");
            Console.WriteLine();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"❌ Error getting wallet details: {ex.Message}");
        }

        // Ожидание перед выходом
        await Task.Delay(2000);
        steamUser!.LogOff();
    }

    static void OnLoggedOff(SteamUser.LoggedOffCallback callback)
    {
        Console.WriteLine($"Logged off of Steam: {callback.Result}");
        isRunning = false;
    }
}
