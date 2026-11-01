/* Test-only synthetic CLI. No network, credentials, tools or interpreter. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static char control[16384];
static const char *value(const char *key) {
    static char pattern[128];
    snprintf(pattern,sizeof(pattern),"\"%s\"",key);
    char *p=strstr(control,pattern);
    if (!p || !(p=strchr(p,':'))) return NULL;
    do { p++; } while (*p==' ');
    return p;
}
static void json_text(const char *p,const char *fallback) {
    if (!p || *p!='"') { printf("\"%s\"",fallback); return; }
    putchar(*p++);
    while (*p) {
        char c=*p++; putchar(c);
        if (c=='\\' && *p) putchar(*p++);
        else if (c=='"') return;
    }
    exit(2);
}
int main(int argc,char **argv) {
    const char *session=NULL,*model=NULL,*runtime=NULL,*profile=NULL,*speaker=NULL;
    for (int i=1;i+1<argc;i++) {
        if (!strcmp(argv[i],"--session-id")) session=argv[i+1];
        if (!strcmp(argv[i],"--model")) model=argv[i+1];
        if (!strcmp(argv[i],"--runtime-sha256")) runtime=argv[i+1];
        if (!strcmp(argv[i],"--profile")) profile=argv[i+1];
    }
    if (!session || !model) return 2;
    while (getchar()!=EOF) {}
    char control_path[4096];
    snprintf(control_path,sizeof(control_path),"%s/control.json",profile ? profile : ".");
    FILE *f=fopen(control_path,"r");
    if (f) { size_t n=fread(control,1,sizeof(control)-1,f);control[n]=0;fclose(f); }
    f=fopen("called","w"); if (!f) return 2; fputs("yes",f); fclose(f);
    const char *v=value("sleep"); if (v && !strncmp(v,"true",4)) sleep(60);
    v=value("session"); if (v && !strncmp(v,"true",4)) session="wrong-session";
    v=value("tools"); const char *tools=v && strstr(v,"Bash") ? "[\"Bash\"]" : "[]";
    if (runtime) {
        speaker=value("speaker");
        printf("{\"schema\":\"pelago.hermes.v1\",\"credential_mode\":\"profile-access-only-no-copy\",\"requested_model\":\"%s\",\"session_id\":\"%s\",\"runtime_sha256\":\"%s\",\"speaker_sha256\":",model,session,runtime);
        json_text(speaker, "missing");
        printf(",\"runtime_guard\":true,\"title_generation\":false,\"tools\":%s,\"tool_attempts\":0,\"text\":",tools);
        json_text(value("text"), "simulated hermes answer");
        printf(",\"calls\":[{\"requested\":\"%s\",\"provider\":\"nvidia\",\"source\":\"sdk.response.model\",\"session_id\":\"%s\",\"tools\":[],\"served\":",model,session);
        json_text(value("served"),model); printf("}]}\n"); return 0;
    }
    printf("{\"type\":\"system\",\"subtype\":\"init\",\"tools\":%s,\"mcp_servers\":[],\"session_id\":\"%s\"}\n",tools,session);
    printf("{\"type\":\"result\",\"is_error\":false,\"result\":");
    json_text(value("text"),"simulated answer");
    printf(",\"modelUsage\":{");json_text(value("served"),model);
    printf(":{}},\"session_id\":\"%s\"}\n",session);
    return 0;
}
