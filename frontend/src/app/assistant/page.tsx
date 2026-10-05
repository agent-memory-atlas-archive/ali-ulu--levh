"use client";

// The memory assistant: an Opengeni agent that answers from this store.
//
// The chat UI talks only to this dashboard's own server (`/api/opengeni`),
// which holds the organization key, decides the session's agent and privacy,
// and attaches the memories LEVH recalled for each message. Nothing in the
// browser may see that key or reach Opengeni directly — which is the whole
// reason this is a proxied page and not a widget with a token in it.
import { OpenGeniChat } from "@opengeni/react/session-ui";
import "@opengeni/react/compiled.css";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/lib/i18n";
import { getToken, TOKEN_HEADER } from "@/lib/token";

export default function AssistantPage() {
  const t = useT();
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t("assistant.title")}</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{t("assistant.subtitle")}</p>
        </CardContent>
      </Card>
      <OpenGeniChat
        baseUrl="/api/opengeni"
        // A locked-down install gates /api/* behind LEVH_TOKEN, and the proxy
        // reads the same header the rest of the dashboard sends. Called per
        // request, so a token pasted after load is picked up.
        headers={() => {
          const token = getToken();
          const sessionHeaders: Record<string, string> = {};
          if (token) sessionHeaders[TOKEN_HEADER] = token;
          return sessionHeaders;
        }}
        className="h-[calc(100vh-320px)] min-h-[520px]"
        labels={{
          newChatTitle: t("assistant.chat.newChatTitle"),
          newChatPlaceholder: t("assistant.chat.newChatPlaceholder"),
          send: t("assistant.chat.send"),
          openChats: t("assistant.chat.openChats"),
          closeChats: t("assistant.chat.closeChats"),
          newChatUnavailable: t("assistant.chat.newChatUnavailable"),
        }}
      />
    </div>
  );
}
