using System.Text.Json;
using SteamKit2;
using SteamKit2.Authentication;
using SteamKit2.Internal;

namespace SteamWalletChecker;

class Program
{
    static SteamClient? steamClient;
    static CallbackManager? manager;
    static SteamUser? steamUser;
    static SteamUnifiedMessages? steamUnifiedMessages;
    static bool isRunning = true;
    static string? username;
    static string? password;

    static void Main(string[] args)
    {
        Console.WriteLine("=== Steam Wallet Checker ===");
        Console.WriteLine();

        // Получение учетных данных
        Console.Write("Enter Steam username: ");
        username = Console.ReadLine();

        Console.Write("Enter Steam password: ");
        password = ReadPassword();
        Console.WriteLine();

        if (string.IsNullOrEmpty(username) || string.IsNullOrEmpty(password))
        {
            Console.WriteLine("Error: Username and password are required!");
            return;
        }

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

        Console.WriteLine("Connecting to Steam...");
        steamClient.Connect();

        // Основной цикл обработки событий
        while (isRunning)
        {
            manager.RunWaitCallbacks(TimeSpan.FromSeconds(1));
        }

        Console.WriteLine();
        Console.WriteLine("Press any key to exit...");
        Console.ReadKey();
    }

    static async void OnConnected(SteamClient.ConnectedCallback callback)
    {
        Console.WriteLine($"Connected to Steam! Logging in as '{username}'...");

        try
        {
            var authSession = await steamClient!.Authentication.BeginAuthSessionViaCredentialsAsync(new AuthSessionDetails
            {
                Username = username!,
                Password = password!,
                Authenticator = new UserConsoleAuthenticator(),
            });

            var pollResponse = await authSession.PollingWaitForResultAsync();

            steamUser!.LogOn(new SteamUser.LogOnDetails
            {
                Username = pollResponse.AccountName,
                AccessToken = pollResponse.RefreshToken,
            });
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Authentication error: {ex.Message}");
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
                Console.WriteLine("Unable to logon to Steam: This account is SteamGuard protected.");
                Console.WriteLine("Please ensure you handle the 2FA/email code correctly.");
                isRunning = false;
                return;
            }

            Console.WriteLine($"Unable to logon to Steam: {callback.Result} / {callback.ExtendedResult}");
            isRunning = false;
            return;
        }

        Console.WriteLine("Successfully logged on!");
        Console.WriteLine();

        try
        {
            // Получение информации о кошельке
            var userService = steamUnifiedMessages!.CreateService<UserAccount>();

            var wallet = await userService.GetClientWalletDetails(new CUserAccount_GetClientWalletDetails_Request
            {
                include_balance_in_usd = true,
                include_formatted_balance = true
            });

            Console.WriteLine("=== Wallet Information ===");
            Console.WriteLine(JsonSerializer.Serialize(wallet, new JsonSerializerOptions
            {
                WriteIndented = true
            }));
            Console.WriteLine();

            // Вывод основной информации
            if (wallet.Body != null)
            {
                Console.WriteLine($"Balance: {wallet.Body.formatted_balance}");
                Console.WriteLine($"Country: {wallet.Body.wallet_country_code}");
                Console.WriteLine($"Currency Code: {wallet.Body.currency_code}");
                Console.WriteLine($"Balance in USD: ${wallet.Body.balance_in_usd / 100.0:F2}");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Error getting wallet details: {ex.Message}");
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

    // Утилита для скрытого ввода пароля
    static string ReadPassword()
    {
        string password = "";
        ConsoleKeyInfo key;

        do
        {
            key = Console.ReadKey(true);

            if (key.Key != ConsoleKey.Backspace && key.Key != ConsoleKey.Enter)
            {
                password += key.KeyChar;
                Console.Write("*");
            }
            else if (key.Key == ConsoleKey.Backspace && password.Length > 0)
            {
                password = password[0..^1];
                Console.Write("\b \b");
            }
        }
        while (key.Key != ConsoleKey.Enter);

        return password;
    }
}
