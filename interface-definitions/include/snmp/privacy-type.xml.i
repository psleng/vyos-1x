<!-- include start from snmp/privacy-type.xml.i -->
<leafNode name="type">
  <properties>
    <help>Defines the protocol for privacy</help>
    <completionHelp>
      <list>des aes aes192 aes256</list>
    </completionHelp>
    <valueHelp>
      <format>des</format>
      <description>Data Encryption Standard</description>
    </valueHelp>
    <valueHelp>
      <format>aes</format>
      <description>Advanced Encryption Standard, 128-bit (AES-128)</description>
    </valueHelp>
    <valueHelp>
      <format>aes192</format>
      <description>Advanced Encryption Standard, 192-bit (AES-192)</description>
    </valueHelp>
    <valueHelp>
      <format>aes256</format>
      <description>Advanced Encryption Standard, 256-bit (AES-256)</description>
    </valueHelp>
    <constraint>
      <regex>(des|aes|aes192|aes256)</regex>
    </constraint>
  </properties>
  <defaultValue>des</defaultValue>
</leafNode>
<!-- include end -->
