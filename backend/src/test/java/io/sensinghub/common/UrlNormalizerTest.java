package io.sensinghub.common;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class UrlNormalizerTest {

    @Test
    void articleUrlBecomesDomain() {
        var n = UrlNormalizer.normalize("https://daily.hankooki.com/news/articleView.html?idxno=1415376");
        assertThat(n.domain()).isEqualTo("daily.hankooki.com");
        assertThat(n.baseUrl()).isEqualTo("https://daily.hankooki.com");
    }

    @Test
    void schemeIsAddedAndWwwIsDroppedFromKeyOnly() {
        var n = UrlNormalizer.normalize("  WWW.Example.COM/path ");
        assertThat(n.domain()).isEqualTo("example.com");
        assertThat(n.baseUrl()).isEqualTo("https://www.example.com");
    }

    @Test
    void nonDefaultPortIsKept() {
        var n = UrlNormalizer.normalize("http://news.example.org:8080/a");
        assertThat(n.baseUrl()).isEqualTo("http://news.example.org:8080");
    }

    @Test
    void koreanDomainIsPunycoded() {
        var n = UrlNormalizer.normalize("https://한국.kr");
        assertThat(n.domain()).startsWith("xn--");
    }

    @ParameterizedTest
    @ValueSource(strings = {
            "http://localhost:8080", "http://127.0.0.1", "http://10.1.2.3/x", "http://192.168.0.1",
            "http://172.20.0.5", "http://169.254.169.254/latest", "http://intranet", "http://db.internal",
            "ftp://example.com", "javascript:alert(1)", "https://user:pw@example.com", "", "   ",
            "http://127.1", "http://0x7f.0.0.1", "https://a..com", "https://example.com:99999"
    })
    void rejectsInternalOrInvalid(String input) {
        assertThatThrownBy(() -> UrlNormalizer.normalize(input))
                .isInstanceOf(UrlNormalizer.InvalidUrlException.class);
    }

    @Test
    void publicIpIsAllowed() {
        assertThat(UrlNormalizer.normalize("http://8.8.8.8").domain()).isEqualTo("8.8.8.8");
    }
}
