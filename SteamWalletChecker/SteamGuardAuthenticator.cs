using System;
using System.Security.Cryptography;
using System.Text;
using SteamKit2.Authentication;

namespace SteamWalletChecker;

/// <summary>
/// Аутентификатор Steam Guard с автоматической генерацией 2FA кодов
/// </summary>
public class SteamGuardAuthenticator : IAuthenticator
{
    private readonly string? sharedSecret;
    private readonly string? identitySecret;
    private readonly bool useManualInput;

    private static readonly char[] steamGuardCodeChars = new char[]
    {
        '2', '3', '4', '5', '6', '7', '8', '9',
        'B', 'C', 'D', 'F', 'G', 'H', 'J', 'K',
        'M', 'N', 'P', 'Q', 'R', 'T', 'V', 'W',
        'X', 'Y'
    };

    public SteamGuardAuthenticator(string? sharedSecret = null, string? identitySecret = null)
    {
        this.sharedSecret = sharedSecret;
        this.identitySecret = identitySecret;
        this.useManualInput = string.IsNullOrEmpty(sharedSecret);

        if (!useManualInput)
        {
            Console.WriteLine("Using automatic 2FA code generation");
        }
    }

    /// <summary>
    /// Получение кода устройства (2FA код)
    /// </summary>
    public Task<string> GetDeviceCodeAsync(bool previousCodeWasIncorrect)
    {
        if (previousCodeWasIncorrect)
        {
            Console.WriteLine("⚠️ Previous code was incorrect!");
        }

        if (useManualInput)
        {
            // Ручной ввод кода
            Console.Write("Enter Steam Guard code: ");
            var code = Console.ReadLine();
            return Task.FromResult(code ?? string.Empty);
        }
        else
        {
            // Автоматическая генерация кода
            var code = GenerateSteamGuardCode();
            Console.WriteLine($"🔐 Generated 2FA code: {code}");
            return Task.FromResult(code);
        }
    }

    /// <summary>
    /// Подтверждение на устройстве (для login approval)
    /// </summary>
    public Task<bool> AcceptDeviceConfirmationAsync()
    {
        Console.WriteLine("✓ Device confirmation accepted");
        return Task.FromResult(true);
    }

    /// <summary>
    /// Получение email кода
    /// </summary>
    public Task<string> GetEmailCodeAsync(string email, bool previousCodeWasIncorrect)
    {
        if (previousCodeWasIncorrect)
        {
            Console.WriteLine("⚠️ Previous email code was incorrect!");
        }

        Console.Write($"Enter email code sent to {email}: ");
        var code = Console.ReadLine();
        return Task.FromResult(code ?? string.Empty);
    }

    /// <summary>
    /// Генерация Steam Guard кода на основе shared_secret
    /// </summary>
    private string GenerateSteamGuardCode()
    {
        if (string.IsNullOrEmpty(sharedSecret))
        {
            throw new InvalidOperationException("Shared secret is not set");
        }

        try
        {
            // Декодирование Base64 секрета
            byte[] sharedSecretBytes = Convert.FromBase64String(sharedSecret);

            // Получение текущего времени Steam
            long timeStamp = GetSteamTime();

            // Создание HMAC-SHA1
            using var hmac = new HMACSHA1(sharedSecretBytes);
            byte[] timeBytes = BitConverter.GetBytes(timeStamp);

            if (BitConverter.IsLittleEndian)
            {
                Array.Reverse(timeBytes);
            }

            byte[] hash = hmac.ComputeHash(timeBytes);

            // Извлечение кода
            int start = hash[19] & 0x0f;
            byte[] bytes = new byte[4];
            Array.Copy(hash, start, bytes, 0, 4);

            if (BitConverter.IsLittleEndian)
            {
                Array.Reverse(bytes);
            }

            uint fullCode = BitConverter.ToUInt32(bytes, 0) & 0x7fffffff;

            // Конвертация в Steam Guard формат
            StringBuilder code = new StringBuilder();
            for (int i = 0; i < 5; i++)
            {
                code.Append(steamGuardCodeChars[fullCode % steamGuardCodeChars.Length]);
                fullCode /= (uint)steamGuardCodeChars.Length;
            }

            return code.ToString();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"❌ Error generating 2FA code: {ex.Message}");
            Console.Write("Enter Steam Guard code manually: ");
            return Console.ReadLine() ?? string.Empty;
        }
    }

    /// <summary>
    /// Получение текущего времени Steam (Unix timestamp / 30)
    /// </summary>
    private long GetSteamTime()
    {
        // Steam использует Unix timestamp деленный на 30 секунд
        long unixTime = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        return unixTime / 30L;
    }

    /// <summary>
    /// Генерация confirmation key для подтверждения трейдов
    /// (использует identity_secret)
    /// </summary>
    public string GenerateConfirmationKey(long time, string tag = "conf")
    {
        if (string.IsNullOrEmpty(identitySecret))
        {
            throw new InvalidOperationException("Identity secret is not set");
        }

        try
        {
            byte[] identitySecretBytes = Convert.FromBase64String(identitySecret);
            byte[] dataBytes = Encoding.UTF8.GetBytes($"{time}{tag}");

            using var hmac = new HMACSHA1(identitySecretBytes);
            byte[] hash = hmac.ComputeHash(dataBytes);

            return Convert.ToBase64String(hash);
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException($"Failed to generate confirmation key: {ex.Message}");
        }
    }

    /// <summary>
    /// Проверка корректности shared_secret
    /// </summary>
    public bool ValidateSharedSecret()
    {
        if (string.IsNullOrEmpty(sharedSecret))
        {
            return false;
        }

        try
        {
            Convert.FromBase64String(sharedSecret);
            var code = GenerateSteamGuardCode();
            return !string.IsNullOrEmpty(code) && code.Length == 5;
        }
        catch
        {
            return false;
        }
    }
}
