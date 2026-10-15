package io.sensinghub.common;

import java.net.IDN;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;
import java.util.regex.Pattern;

/**
 * 사용자가 입력한 주소(기사 주소 포함)를 등록 단위인 도메인으로 정규화한다.
 *
 * <p>예: {@code https://daily.hankooki.com/news/articleView.html?idxno=1415376}
 * → domain {@code daily.hankooki.com}, baseUrl {@code https://daily.hankooki.com}
 *
 * <p>사용자가 임의 주소를 등록하므로 내부망 주소(localhost, 사설 IP 등)는 거부한다.
 * DNS가 사설 IP로 풀리는 경우는 워커가 요청 시점에 한 번 더 막는다.
 */
public final class UrlNormalizer {

    private static final Pattern IPV4 = Pattern.compile("^\\d{1,3}(\\.\\d{1,3}){3}$");
    private static final Pattern HOST_LABEL = Pattern.compile("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$");

    private UrlNormalizer() {
    }

    public record Normalized(String domain, String baseUrl) {
    }

    public static Normalized normalize(String input) {
        if (input == null || input.isBlank()) {
            throw new InvalidUrlException("주소를 입력해 주세요.");
        }
        String raw = input.strip();
        if (!raw.contains("://")) {
            raw = "https://" + raw;
        }

        URI uri;
        try {
            uri = new URI(raw);
        } catch (URISyntaxException e) {
            throw new InvalidUrlException("주소 형식이 올바르지 않습니다.");
        }

        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        if (!scheme.equals("http") && !scheme.equals("https")) {
            throw new InvalidUrlException("http 또는 https 주소만 등록할 수 있습니다.");
        }
        if (uri.getUserInfo() != null) {
            throw new InvalidUrlException("계정 정보가 포함된 주소는 등록할 수 없습니다.");
        }

        String host = uri.getHost();
        if (host == null && uri.getAuthority() != null && !uri.getAuthority().contains("@")) {
            // 한글 도메인 등 비ASCII 호스트는 java.net.URI가 host로 인식하지 못한다
            String authority = uri.getAuthority();
            int colon = authority.lastIndexOf(':');
            host = colon > 0 && authority.substring(colon + 1).matches("\\d+") ? authority.substring(0, colon) : authority;
        }
        if (host == null || host.isBlank()) {
            throw new InvalidUrlException("주소에서 도메인을 찾을 수 없습니다.");
        }
        try {
            host = IDN.toASCII(host.toLowerCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            throw new InvalidUrlException("도메인 형식이 올바르지 않습니다.");
        }
        if (host.endsWith(".")) {
            host = host.substring(0, host.length() - 1);
        }
        rejectInternal(host);

        String domain = host.startsWith("www.") ? host.substring(4) : host;
        int port = uri.getPort();
        if (port > 65535) {
            throw new InvalidUrlException("포트 번호가 올바르지 않습니다.");
        }
        boolean defaultPort = port == -1 || (scheme.equals("https") && port == 443) || (scheme.equals("http") && port == 80);
        String baseUrl = scheme + "://" + host + (defaultPort ? "" : ":" + port);
        return new Normalized(domain, baseUrl);
    }

    private static void rejectInternal(String host) {
        if (host.equals("localhost") || host.endsWith(".localhost") || host.endsWith(".local")
                || host.endsWith(".internal") || host.startsWith("[") || host.contains(":")) {
            throw new InvalidUrlException("내부망 주소는 등록할 수 없습니다.");
        }
        if (IPV4.matcher(host).matches()) {
            String[] p = host.split("\\.");
            int a = Integer.parseInt(p[0]);
            int b = Integer.parseInt(p[1]);
            boolean internal = a == 0 || a == 10 || a == 127
                    || (a == 169 && b == 254)
                    || (a == 172 && b >= 16 && b <= 31)
                    || (a == 192 && b == 168)
                    || (a == 100 && b >= 64 && b <= 127);
            if (internal) {
                throw new InvalidUrlException("내부망 주소는 등록할 수 없습니다.");
            }
            return;
        }
        if (!host.contains(".")) {
            throw new InvalidUrlException("도메인 형식이 올바르지 않습니다.");
        }
        String[] labels = host.split("\\.");
        for (String label : labels) {
            if (!HOST_LABEL.matcher(label).matches()) {
                throw new InvalidUrlException("도메인 형식이 올바르지 않습니다.");
            }
        }
        // 127.1, 0x7f.0.0.1 처럼 IP를 줄여 쓴 형태는 도메인이 아니다
        String last = labels[labels.length - 1];
        if (last.matches("\\d+|0x[0-9a-f]*")) {
            throw new InvalidUrlException("내부망 주소는 등록할 수 없습니다.");
        }
    }

    public static class InvalidUrlException extends ApiException {
        public InvalidUrlException(String message) {
            super(org.springframework.http.HttpStatus.BAD_REQUEST, "INVALID_URL", message);
        }
    }
}
