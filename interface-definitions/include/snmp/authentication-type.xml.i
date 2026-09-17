<!-- include start from snmp/authentication-type.xml.i -->
<leafNode name="type">
  <properties>
    <help>Define used protocol</help>
    <completionHelp>
      <list>md5 sha sha224 sha256 sha384 sha512</list>
    </completionHelp>
    <valueHelp>
      <format>md5</format>
      <description>Message Digest 5</description>
    </valueHelp>
    <valueHelp>
      <format>sha</format>
      <description>Secure Hash Algorithm 1 (SHA-1)</description>
    </valueHelp>
    <valueHelp>
      <format>sha224</format>
      <description>Secure Hash Algorithm 2, 224-bit (SHA-224)</description>
    </valueHelp>
    <valueHelp>
      <format>sha256</format>
      <description>Secure Hash Algorithm 2, 256-bit (SHA-256)</description>
    </valueHelp>
    <valueHelp>
      <format>sha384</format>
      <description>Secure Hash Algorithm 2, 384-bit (SHA-384)</description>
    </valueHelp>
    <valueHelp>
      <format>sha512</format>
      <description>Secure Hash Algorithm 2, 512-bit (SHA-512)</description>
    </valueHelp>
    <constraint>
      <regex>(md5|sha|sha224|sha256|sha384|sha512)</regex>
    </constraint>
  </properties>
  <defaultValue>md5</defaultValue>
</leafNode>
<!-- include end -->
