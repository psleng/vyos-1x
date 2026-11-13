<!-- SMS gateway: inert until an allowlist and password are configured. -->
<node name="sms-command">
  <properties><help>Authenticated operational commands over SMS</help></properties>
  <children>
    <leafNode name="password">
      <properties><help>Shared password (hashed on commit; required in every SMS)</help><secret/></properties>
    </leafNode>
    <leafNode name="password-hash">
      <properties><help>Salted modular-crypt password hash</help><secret/></properties>
    </leafNode>
    <leafNode name="allow-from">
      <properties><help>Authorized sender in E.164 format (+countrycode and number, at most 15 digits)</help><multi/></properties>
    </leafNode>
    <leafNode name="rate-limit">
      <properties>
        <help>Maximum admitted requests per minute per sender (including CONFIRM)</help>
        <valueHelp><format>positive integer</format><description>Maximum admitted requests per minute per sender (including CONFIRM)</description></valueHelp>
        <constraint><regex>[1-9][0-9]*</regex></constraint>
      </properties>
      <defaultValue>5</defaultValue>
    </leafNode>
    <leafNode name="timeout">
      <properties>
        <help>Command execution timeout in seconds</help>
        <valueHelp><format>positive integer</format><description>Command execution timeout in seconds</description></valueHelp>
        <constraint><regex>[1-9][0-9]*</regex></constraint>
      </properties>
      <defaultValue>20</defaultValue>
    </leafNode>
    <leafNode name="max-reply-segments">
      <properties>
        <help>Maximum SMS segment budget per request</help>
        <valueHelp><format>positive integer</format><description>Maximum SMS segment budget per request</description></valueHelp>
        <constraint><regex>[1-9][0-9]*</regex></constraint>
      </properties>
      <defaultValue>4</defaultValue>
    </leafNode>
    <node name="privileged">
      <properties><help>Action verbs (each requires password and two-step confirmation)</help></properties>
      <children>
        <leafNode name="verb">
          <properties>
            <help>Enable a privileged operational verb</help>
            <completionHelp><script>${vyos_completion_dir}/list_sms_command_verbs.py</script></completionHelp>
            <multi/>
          </properties>
        </leafNode>
        <leafNode name="allow-from">
          <properties><help>Additional E.164 sender restriction for action verbs</help><multi/></properties>
        </leafNode>
      </children>
    </node>
  </children>
</node>
