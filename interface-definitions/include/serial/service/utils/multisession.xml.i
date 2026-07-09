<!-- include start from serial/service/utils/multisession.xml.i -->
<leafNode name="multi-session-limit">
  <properties>
    <help>Multi-session limit</help>
    <valueHelp>
      <format>u32:0-16</format>
      <description>Integer</description>
    </valueHelp>
    <constraint>
      <validator name="numeric" argument="--range 0-16"/>
    </constraint>
  </properties>
</leafNode>
<!-- include end -->
