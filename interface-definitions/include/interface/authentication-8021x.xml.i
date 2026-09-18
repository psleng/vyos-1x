<!-- include start from interface/authentication-8021x.xml.i -->
<node name="authentication">
  <properties>
    <help>IEEE 802.1X port-based network access control (authenticator role)</help>
  </properties>
  <children>
    #include <include/radius-server-ipv4-ipv6.xml.i>
    <node name="eap-server">
      <properties>
        <help>Local EAP-TLS authentication server (standalone, no external RADIUS)</help>
      </properties>
      <children>
        #include <include/pki/ca-certificate-multi.xml.i>
        #include <include/pki/certificate-key.xml.i>
      </children>
    </node>
    <node name="macsec">
      <properties>
        <help>Distribute MACsec keys to authenticated peers (MKA key server)</help>
      </properties>
      <children>
        <leafNode name="cipher">
          <properties>
            <help>MACsec cipher suite</help>
            <completionHelp>
              <list>gcm-aes-128 gcm-aes-256</list>
            </completionHelp>
            <valueHelp>
              <format>gcm-aes-128</format>
              <description>Galois/Counter Mode of AES cipher with 128-bit key</description>
            </valueHelp>
            <valueHelp>
              <format>gcm-aes-256</format>
              <description>Galois/Counter Mode of AES cipher with 256-bit key</description>
            </valueHelp>
            <constraint>
              <regex>(gcm-aes-128|gcm-aes-256)</regex>
            </constraint>
          </properties>
          <defaultValue>gcm-aes-128</defaultValue>
        </leafNode>
        <leafNode name="encrypt">
          <properties>
            <help>Enable MACsec confidentiality (integrity-only when unset)</help>
            <valueless/>
          </properties>
        </leafNode>
        <node name="mka">
          <properties>
            <help>MACsec Key Agreement protocol (MKA) parameters</help>
          </properties>
          <children>
            <leafNode name="priority">
              <properties>
                <help>MKA actor priority (lower value wins key-server election)</help>
                <valueHelp>
                  <format>u32:0-255</format>
                  <description>MKA priority</description>
                </valueHelp>
                <constraint>
                  <validator name="numeric" argument="--range 0-255"/>
                </constraint>
              </properties>
              <defaultValue>0</defaultValue>
            </leafNode>
            <leafNode name="cak">
              <properties>
                <help>Pre-shared Connectivity Association Key (enables PSK key-server mode)</help>
                <valueHelp>
                  <format>txt</format>
                  <description>32 hex-digits for gcm-aes-128 or 64 hex-digits for gcm-aes-256</description>
                </valueHelp>
                <constraint>
                  <regex>[A-Fa-f0-9]{32}</regex>
                  <regex>[A-Fa-f0-9]{64}</regex>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="ckn">
              <properties>
                <help>Connectivity Association Key Name</help>
                <valueHelp>
                  <format>txt</format>
                  <description>2..64 hex-digits (1..32 bytes)</description>
                </valueHelp>
                <constraint>
                  <regex>[A-Fa-f0-9]{2,64}</regex>
                </constraint>
              </properties>
            </leafNode>
          </children>
        </node>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
