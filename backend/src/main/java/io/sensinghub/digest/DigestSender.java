package io.sensinghub.digest;

import java.time.Duration;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.mail.javamail.JavaMailSender;
import org.springframework.mail.javamail.MimeMessageHelper;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import jakarta.mail.MessagingException;
import jakarta.mail.internet.MimeMessage;

/**
 * 다이제스트를 실제로 보낸다.
 * <ul>
 *   <li>EMAIL: {@link JavaMailSender}(spring.mail.*, 환경 변수 SENSING_MAIL_*). HTML 본문. 호스트가 비어 있으면
 *       {@link NotSentException}("메일 서버가 설정되지 않았습니다")로 끝내고 호출자가 FAILED로 남긴다.</li>
 *   <li>WEBHOOK: {@code {"text": <텍스트 본문>}} JSON POST (Slack 수신 웹훅·Mattermost 형식. Teams 등 다른 형식은 후속).
 *       제한 시간 10초, 2xx가 아니면 실패.</li>
 * </ul>
 * 비밀(SMTP 비밀번호)은 어디에도 남기지 않는다: 이 클래스는 비밀번호를 읽지 않으며 오류 메시지는 예외의 메시지만 옮긴다.
 */
@Component
public class DigestSender {

    private static final Logger log = LoggerFactory.getLogger(DigestSender.class);

    /** 보내지 못했다. 메시지는 digest_run.error에 그대로 남는다 (비밀 없음). */
    public static class NotSentException extends RuntimeException {
        public NotSentException(String message) {
            super(message);
        }
    }

    private final JavaMailSender mail;
    private final String mailHost;
    private final String from;
    private final RestClient webhook;

    @Autowired
    public DigestSender(ObjectProvider<JavaMailSender> mail,
                        @Value("${spring.mail.host:}") String mailHost,
                        @Value("${sensing.digest.from:}") String from,
                        @Value("${spring.mail.username:}") String mailUsername,
                        @Value("${sensing.digest.webhook-timeout-seconds:10}") int webhookTimeoutSeconds) {
        this(mail.getIfAvailable(), mailHost, from.isBlank() ? mailUsername : from, webhookTimeoutSeconds);
    }

    /** 테스트용: 메일 설정 없음(mailHost 빈 문자열)을 흉내 낼 수 있다. */
    public DigestSender(JavaMailSender mail, String mailHost, String from, int webhookTimeoutSeconds) {
        this.mail = mail;
        this.mailHost = mailHost == null ? "" : mailHost.strip();
        this.from = from == null ? "" : from.strip();
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(webhookTimeoutSeconds));
        factory.setReadTimeout(Duration.ofSeconds(webhookTimeoutSeconds));
        this.webhook = RestClient.builder().requestFactory(factory).build();
    }

    public boolean mailConfigured() {
        return mail != null && !mailHost.isBlank();
    }

    public void send(String channel, String target, DigestComposer.Digest digest) {
        switch (channel) {
            case "EMAIL" -> sendMail(target, digest);
            case "WEBHOOK" -> postWebhook(target, digest);
            default -> throw new NotSentException("알 수 없는 채널입니다: " + channel);
        }
    }

    private void sendMail(String to, DigestComposer.Digest digest) {
        if (!mailConfigured()) {
            throw new NotSentException("메일 서버가 설정되지 않았습니다 (SENSING_MAIL_HOST).");
        }
        try {
            MimeMessage message = mail.createMimeMessage();
            MimeMessageHelper helper = new MimeMessageHelper(message, false, "UTF-8");
            helper.setTo(to);
            if (!from.isBlank()) {
                helper.setFrom(from);
            }
            helper.setSubject(digest.subject());
            helper.setText(digest.html(), true);
            mail.send(message);
        } catch (MessagingException | org.springframework.mail.MailException e) {
            log.warn("digest mail to {} failed: {}", to, e.getMessage());
            throw new NotSentException("메일 전송 실패: " + brief(e));
        }
    }

    private void postWebhook(String url, DigestComposer.Digest digest) {
        try {
            webhook.post().uri(url).contentType(MediaType.APPLICATION_JSON)
                    .body(Map.of("text", digest.text()))
                    .retrieve().toBodilessEntity();
        } catch (RestClientResponseException e) {
            log.warn("digest webhook {} answered {}", url, e.getStatusCode().value());
            throw new NotSentException("웹훅 응답 " + e.getStatusCode().value());
        } catch (org.springframework.web.client.RestClientException e) {
            log.warn("digest webhook {} failed: {}", url, e.getMessage());
            throw new NotSentException("웹훅 호출 실패: " + brief(e));
        }
    }

    private static String brief(Exception e) {
        String m = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
        return m.length() > 300 ? m.substring(0, 300) : m;
    }
}
